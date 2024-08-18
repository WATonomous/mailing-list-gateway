import os
import re
import time
import urllib.parse
from contextlib import asynccontextmanager
from smtplib import SMTP
from textwrap import dedent

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from watcloud_utils.fastapi import WATcloudFastAPI
from watcloud_utils.logging import logger, set_up_logging

from utils import get_azure_table_client, random_str

scheduler = BackgroundScheduler()
scheduler.start()


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

table_client = get_azure_table_client("signups", create_table_if_not_exists=True)


class SignUpRequest(BaseModel):
    mailing_list: str
    email: str


CODE_TTL_SEC = 15 * 60


@app.post("/sign_up")
def sign_up(req: SignUpRequest, request: Request):
    # validate email
    if not re.match(r"[^@]+@[^@]+\.[^@]+", req.email):
        raise HTTPException(status_code=400, detail="Invalid email")

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

    smtp = SMTP(os.environ["SMTP_HOST"], port=os.environ["SMTP_PORT"])
    smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
    smtp.sendmail(
        os.environ["SMTP_USERNAME"],
        req.email,
        dedent(
            f"""
            Subject: Confirm Your Email Subscription for '{req.mailing_list}'
            From: {os.environ["SMTP_USERNAME"]}
            To: {req.email}
            Reply-To: {os.environ["SMTP_USERNAME"]}
            MIME-Version: 1.0
            Content-Type: text/html; charset="utf-8"

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
            <body>
                <h1>Confirm Your Email</h1>
                <p>Please confirm your email address by clicking the button or the link below to continue receiving updates from '{req.mailing_list}':</p>
                <a href="{confirmation_url}">Confirm Email</a>
                <p>If the button above does not work, please copy and paste the following URL into your browser:</p>
                <p class="link-text">{confirmation_url}</p>
                <p>If you did not request this subscription, no further action is required.</p>
            </body>
            </html>
            """
        ),
    )

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

    # TODO: Add email to mailing list

    # delete the entity
    table_client.delete_entity(partition_key=mailing_list, row_key=email)

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
    msg = f"cleanup: Deleted {deleted_count} expired signup(s)."
    logger.info(msg)
    return {"status": "ok", "message": msg}
