import os
import re
import time
import urllib.parse
from contextlib import asynccontextmanager
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser
from smtplib import SMTP, SMTPNotSupportedError
from textwrap import dedent

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from watcloud_utils.fastapi import WATcloudFastAPI
from watcloud_utils.logging import logger, set_up_logging

from google_admin_sdk_utils import DirectoryService
from utils import get_azure_table_client, random_str


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
    yield
    scheduler.shutdown()


def healthcheck(app: WATcloudFastAPI):
    cleanup_delay_threshold = 120
    if time.time() - app.runtime_info["last_cleanup_time"] > cleanup_delay_threshold:
        msg = f"Last cleanup was more than {cleanup_delay_threshold} seconds ago."
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
        "last_cleanup_time": time.time(),
    },
    health_fns=[healthcheck],
)


class SignUpRequest(BaseModel):
    mailing_list: str
    email: str


CODE_TTL_SEC = 15 * 60


@app.post("/sign_up")
def sign_up(req: SignUpRequest, request: Request):
    # validate email
    if not re.match(r"[^@]+@[^@]+\.[^@]+", req.email):
        raise HTTPException(status_code=400, detail="Invalid email")

    if not directory_service.is_whitelisted_group(req.mailing_list):
        raise HTTPException(status_code=400, detail="Invalid mailing list")

    # Generate a random code
    code = random_str(10)

    table_client.upsert_entity(
        entity={
            "PartitionKey": req.mailing_list,
            "RowKey": req.email,
            "CreatedAt": time.time(),
            "Code": code,
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
            <h1>Confirm Your Email</h1>
            <p>Please confirm your email address by clicking the button or the link below to receiving updates from "{req.mailing_list}". This confirmation link will expire in {CODE_TTL_SEC // 60} minutes.</p>
            <a href="{confirmation_url}">Confirm Email</a>
            <p>If the button above does not work, please copy and paste the following URL into your browser:</p>
            <p class="link-text">{confirmation_url}</p>
            <p>If you did not request this subscription, no further action is required.</p>
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
                a {{
                    background-color: #007BFF;
                    color: white;
                    padding: 10px 20px;
                    text-decoration: none;
                    border-radius: 5px;
                    font-size: 18px;
                }}
                a:hover {{
                    background-color: #0056b3;
                }}
                .link-text {{
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
    from azure.core.exceptions import ResourceNotFoundError

    try:
        entity = table_client.get_entity(partition_key=mailing_list, row_key=email)
    except ResourceNotFoundError:
        app.runtime_info["num_failed_confirms"] += 1
        raise HTTPException(status_code=400, detail="Code expired or invalid")

    if entity["Code"] != code or time.time() - entity["CreatedAt"] > CODE_TTL_SEC:
        app.runtime_info["num_failed_confirms"] += 1
        raise HTTPException(status_code=400, detail="Code expired or invalid")

    if not directory_service.is_whitelisted_group(mailing_list):
        raise HTTPException(
            status_code=500, detail="Invalid mailing list found in the database"
        )

    directory_service.insert_member(mailing_list, email)

    # delete the entity
    table_client.delete_entity(partition_key=mailing_list, row_key=email)

    app.runtime_info["num_successful_confirms"] += 1

    return {
        "status": "ok",
        "message": f"Subscription confirmed! '{email}' has been added to the '{mailing_list}' mailing list.",
    }


@app.post("/cleanup")
def cleanup():
    """
    Clean up expired signups.
    """
    expired_entities = table_client.query_entities(
        query_filter=f"CreatedAt lt @ExpiryTime",
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
