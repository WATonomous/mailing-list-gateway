# mailing-list-gateway

A simple service to ask for user confirmation before adding them to a mailing list.
Currently, only Google Groups is supported.

## Getting started

1. Populate `.env` with your configuration. An example is provided in `.env.example`. The default configuration is suitable for development.

    ```bash
    cp .env.example .env
    ```

2. Obtain a Google Cloud Service Account key and save it as `./secrets/google-service-account.json`.
    1. [Create](https://console.cloud.google.com/projectcreate) a Google Cloud project.
    2. Create a service account under the project with no roles.
    3. In the [Google Admin console](https://admin.google.com/), give "Groups Editor" role to the service account.
    4. Enable the [Admin SDK API](https://console.cloud.google.com/apis/library/admin.googleapis.com) in the Google Cloud project.

3. Start the service.

    ```bash
    docker compose up --build
    ```

Now, you can view the API spec at http://localhost:8000/docs.
If you are using the default SMTP configuration, you can view outgoing emails at http://localhost:8025.
