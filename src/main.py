import os
import re
import time
import urllib.parse
from contextlib import asynccontextmanager
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser
from smtplib import SMTP, SMTPNotSupportedError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from azure.core.exceptions import ResourceNotFoundError
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from watcloud_utils.fastapi import WATcloudFastAPI
from watcloud_utils.logging import logger, set_up_logging

from google_admin_sdk_utils import DirectoryService
from utils import get_azure_table_client, random_str, make_azure_table_key


class HTMLTextFilter(HTMLParser):
    """
    Converts HTML to plain text.
    Derived from https://stackoverflow.com/a/55825140/4527337
    """

    text = ""

    def handle_data(self, data):
        self.text += data


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(cleanup, trigger=CronTrigger.from_crontab("* * * * *"))
    scheduler.add_job(commit, trigger=CronTrigger.from_crontab("* * * * *"))
    yield
    scheduler.shutdown()


def healthcheck(app: WATcloudFastAPI):
    healthcheck_threshold_sec = 120
    if time.time() - app.runtime_info["last_cleanup_time"] > healthcheck_threshold_sec:
        msg = f"Last cleanup was more than {healthcheck_threshold_sec} seconds ago."
        logger.error(msg)
        raise HTTPException(status_code=500, detail=msg)
    if time.time() - app.runtime_info["last_commit_time"] > healthcheck_threshold_sec:
        msg = f"Last commit was more than {healthcheck_threshold_sec} seconds ago."
        logger.error(msg)
        raise HTTPException(status_code=500, detail=msg)


set_up_logging()
scheduler = BackgroundScheduler()
scheduler.start()
table_client = get_azure_table_client("signups", create_table_if_not_exists=True)
directory_service = DirectoryService(logger=logger)
app = WATcloudFastAPI(
    logger=logger,
    lifespan=lifespan,
    initial_runtime_info={
        "num_signups": 0,
        "num_successful_confirms": 0,
        "num_failed_confirms": 0,
        "num_expired_signups": 0,
        "num_successful_commits": 0,
        "last_cleanup_time": time.time(),
        "last_commit_time": time.time(),
    },
    health_fns=[healthcheck],
)


class SignUpRequest(BaseModel):
    mailing_list: str
    email: str


CODE_TTL_SEC = 60 * 60 * 24


@app.post("/sign-up")
def sign_up(req: SignUpRequest, request: Request):
    # validate email
    if not re.match(r"[^@]+@[^@]+\.[^@]+", req.email):
        raise HTTPException(status_code=400, detail="Invalid email")

    if not directory_service.is_whitelisted_group(req.mailing_list):
        raise HTTPException(status_code=400, detail="Invalid mailing list")

    # Generate a random code
    code = random_str(32)

    table_client.upsert_entity(
        entity={
            "PartitionKey": make_azure_table_key([req.mailing_list]),
            "RowKey": make_azure_table_key([req.email, code]),
            "CreatedAt": time.time(),
            "MailingList": req.mailing_list,
            "Email": req.email,
        }
    )

    app_url = (
        os.environ.get("APP_URL") or f"{request.url.scheme}://{request.url.netloc}"
    )
    confirmation_url = f"{app_url}/confirm/{req.mailing_list}/{urllib.parse.quote_plus(req.email)}/{code}"

    # Support both HTML and plain text emails
    # https://stackoverflow.com/a/882770/4527337
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Confirm Your Email Subscription for '{req.mailing_list}'"
    msg["From"] = os.getenv("SMTP_SEND_AS", os.environ["SMTP_USERNAME"])
    msg["To"] = req.email
    msg["Reply-To"] = os.getenv("SMTP_REPLY_TO", os.environ["SMTP_USERNAME"])

    msg_html_body = f"""
        <body>
            <h1>Confirm Your Subscription</h1>
            <p>Thanks for signing up for updates from "{req.mailing_list}"!</p>
            <p>Please confirm your subscription by clicking the button below. This confirmation email will expire in {int(CODE_TTL_SEC / 60 / 60)} hours.</p>
            <a class="confirmation-button" href="{confirmation_url}">Confirm Email</a>
            <p>If the button above does not work, please copy and paste the following URL into your browser:</p>
            <pre class="monospace-text">{confirmation_url}</pre>
            <p> This email was sent to {req.email}. If you did not request this subscription, no further action is required. You won't be subscribed if you don't click the confirmation link.</p>
        </body>
    """
    msg_html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Email Confirmation</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    line-height: 1.6;
                    margin: 20px;
                    color: #333;
                    background-color: #f4f4f4;
                    padding: 20px;
                }}
                .confirmation-button {{
                    background-color: #007BFF;
                    color: white;
                    padding: 10px 20px;
                    text-decoration: none;
                    border-radius: 5px;
                    font-size: 18px;
                }}
                .confirmation-button:hover {{
                    background-color: #0056b3;
                }}
                .monospace-text {{
                    font-family: 'Courier New', monospace;
                }}
            </style>
        </head>
        {msg_html_body}
        </html>
    """
    msg_html_parser = HTMLTextFilter()
    msg_html_parser.feed(msg_html_body)
    msg_text = msg_html_parser.text

    msg.attach(MIMEText(msg_text, "plain"))
    msg.attach(MIMEText(msg_html, "html"))

    with SMTP(os.environ["SMTP_HOST"], port=os.environ["SMTP_PORT"]) as smtp:
        try:
            smtp.starttls()
        except SMTPNotSupportedError as e:
            logger.warning(
                f"SMTP server does not support STARTTLS: {e}. Attempting to send email without encryption."
            )
        smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)

    app.runtime_info["num_signups"] += 1

    return {"status": "ok", "message": f"Confirmation email sent to '{req.email}'."}


@app.get("/confirm/{mailing_list}/{email}/{code}")
def confirm(mailing_list: str, email: str, code: str):
    """
    Confirm the subscription and schedule the addition to the mailing list.
    We schedule the addition instead of adding it immediately to minimize the room
    for error in this handler (e.g., network issues when adding to the mailing list).
    """
    try:
        # update_entity merges the  new entity with the existing entity, and throws
        # ResourceNotFoundError if the entity does not exist.
        table_client.update_entity(
            entity={
                "PartitionKey": make_azure_table_key([mailing_list]),
                "RowKey": make_azure_table_key([email, code]),
                "ConfirmedAt": time.time(),
            }
        )
    except ResourceNotFoundError:
        app.runtime_info["num_failed_confirms"] += 1
        raise HTTPException(status_code=400, detail="Link expired or invalid. Please sign up again.")

    app.runtime_info["num_successful_confirms"] += 1

    return {
        "status": "ok",
        "message": f"Subscription confirmed! Details: {mailing_list=}, {email=}",
    }


@app.post("/cleanup")
def cleanup():
    """
    Clean up expired signups.
    """
    # find unconfirmed signups that are older than CODE_TTL_SEC
    expired_entities = table_client.query_entities(
        query_filter=f"ConfirmedAt eq null and CreatedAt lt @ExpiryTime",
        select=["PartitionKey", "RowKey"],
        parameters={"ExpiryTime": time.time() - CODE_TTL_SEC},
        headers={"Accept": "application/json;odata=nometadata"},
    )
    deleted_count = 0
    for entity in expired_entities:
        table_client.delete_entity(
            partition_key=entity["PartitionKey"], row_key=entity["RowKey"]
        )
        deleted_count += 1

    app.runtime_info["num_expired_signups"] += deleted_count
    app.runtime_info["last_cleanup_time"] = time.time()
    msg = f"cleanup: Deleted {deleted_count} expired signup(s)."
    logger.info(msg)
    return {"status": "ok", "message": msg}

@app.post("/commit")
def commit():
    """
    Add confirmed signups to the mailing list.
    Adding to the mailing list is idempotent, so we can safely retry this operation.
    """
    confirmed_entities = table_client.query_entities(
        query_filter="ConfirmedAt ge 0",
        select=["PartitionKey", "RowKey", "MailingList", "Email"],
        headers={"Accept": "application/json;odata=nometadata"},
    )

    commit_count = 0
    for entity in confirmed_entities:
        mailing_list = entity["MailingList"]
        email = entity["Email"]

        # Sanity check to ensure the mailing list is valid
        if not directory_service.is_whitelisted_group(mailing_list):
            raise HTTPException(
                status_code=500, detail="Invalid mailing list found in the database"
            )

        directory_service.insert_member(mailing_list, email)

        table_client.delete_entity(partition_key=entity["PartitionKey"], row_key=entity["RowKey"])

        commit_count += 1

    app.runtime_info["num_successful_commits"] += commit_count
    app.runtime_info["last_commit_time"] = time.time()

    msg = f"commit: Committed {commit_count} confirmed signup(s) to the mailing list."
    logger.info(msg)
    return {"status": "ok", "message": msg}
