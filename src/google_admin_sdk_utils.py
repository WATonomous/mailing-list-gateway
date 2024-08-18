import logging
import os.path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SERVICE_ACCOUNT_FILE = "/secrets/google-service-account.json"
SCOPES = [
    # Required to get group: https://developers.google.com/admin-sdk/directory/reference/rest/v1/groups/get
    "https://www.googleapis.com/auth/admin.directory.group.readonly",
    # Required to insert member: https://developers.google.com/admin-sdk/directory/reference/rest/v1/members/insert
    "https://www.googleapis.com/auth/admin.directory.group.member",
]

GROUPS_WHITELIST = os.environ["GOOGLE_GROUPS_WHITELIST"].split(",")


class DirectoryService:
    def __init__(self, logger=logging.getLogger(__name__)):
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        self.service = build("admin", "directory_v1", credentials=credentials)

        # Ensure we have permissions to access the groups
        for group_key in GROUPS_WHITELIST:
            self.get_group(group_key)

        self.logger = logger

        logger.info(
            f"DirectoryService initialized with groups whitelist: {GROUPS_WHITELIST}"
        )

    def get_group(self, group_key: str):
        return self.service.groups().get(groupKey=group_key).execute()

    def insert_member(self, group_key: str, email: str):
        try:
            self.service.members().insert(groupKey=group_key, body={"email": email}).execute()
        except HttpError as e:
            if e.resp.status == 409:
                self.logger.warning(f"Member {email} already exists in group {group_key}. Ignoring.")
            else:
                raise

    def is_whitelisted_group(self, group_key: str):
        return group_key in GROUPS_WHITELIST
