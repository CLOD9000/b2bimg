"""DestinationS3 — carica le immagini su S3/MinIO (compatibile AWS S3)."""

from __future__ import annotations

from typing import ClassVar

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.pathutil import sanitize_filename
from plugins.base import (
    ConfigError,
    Destination,
    FieldSpec,
    NetworkError,
    PutResult,
    require,
)
from plugins.registry import register_destination


@register_destination
class DestinationS3(Destination):
    """Bucket S3 o istanza MinIO/S3-compatible."""

    type: ClassVar[str] = "s3"
    label: ClassVar[str] = "S3 / MinIO"
    description: ClassVar[str] = (
        "Upload verso S3, MinIO o qualsiasi compatibile; endpoint e credenziali "
        "configurabili via ${AWS_ACCESS_KEY_ID} / ${AWS_SECRET_ACCESS_KEY}."
    )
    fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("endpoint_url", "Endpoint URL", placeholder="http://minio:9000"),
        FieldSpec("region", "Region", default="us-east-1"),
        FieldSpec("bucket", "Bucket", required=True),
        FieldSpec("prefix", "Prefisso cartella", placeholder="prodotti/"),
        FieldSpec("access_key", "Access key", type="password", env_hint="${AWS_ACCESS_KEY_ID}"),
        FieldSpec("secret_key", "Secret key", type="password", env_hint="${AWS_SECRET_ACCESS_KEY}"),
        FieldSpec(
            "force_path_style",
            "Path-style addressing",
            type="checkbox",
            default=True,
            help="Necessario per MinIO.",
        ),
        FieldSpec("timeout", "Timeout (secondi)", type="number", default="30"),
    )

    def _client(self):  # type: ignore[no-untyped-def]
        bucket = require(self.config, "bucket", "Bucket")
        endpoint = str(self.config.get("endpoint_url") or "").strip() or None
        access_key = str(self.config.get("access_key") or "").strip() or None
        secret_key = str(self.config.get("secret_key") or "").strip() or None
        if bool(access_key) != bool(secret_key):
            raise ConfigError("access key e secret key devono essere impostate insieme")
        try:
            session = boto3.session.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=str(self.config.get("region") or "us-east-1"),
            )
            client = session.client(
                "s3",
                endpoint_url=endpoint,
                use_ssl=not endpoint.startswith("http://") if endpoint else True,
                config=boto3.session.Config(
                    s3={
                        "addressing_style": (
                            "path" if self.config.get("force_path_style", True) else "auto"
                        )
                    },
                    retries={"max_attempts": 2},
                    connect_timeout=int(self.timeout),
                    read_timeout=int(self.timeout),
                ),
            )
            # verifica rapida dei permessi sul bucket
            client.head_bucket(Bucket=bucket)
        except ClientError as exc:
            code = (exc.response.get("Error") or {}).get("Code", "")
            if code in {"404", "NoSuchBucket", "NotFound"}:
                raise ConfigError(f"bucket inesistente: {bucket}") from exc
            raise NetworkError(f"errore S3 su bucket {bucket}: {exc}") from exc
        except (BotoCoreError, ValueError) as exc:
            raise ConfigError(f"configurazione S3 non valida: {exc}") from exc
        return client, bucket

    def _key(self, filename: str) -> str:
        prefix = str(self.config.get("prefix") or "").strip().strip("/")
        return f"{prefix}/{filename}" if prefix else filename

    def put(self, filename: str, data: bytes, conflict: str = "rename") -> PutResult:
        name = sanitize_filename(filename, default="immagine.jpg")
        client, bucket = self._client()
        key = self._key(name)
        exists = True
        try:
            client.head_object(Bucket=bucket, Key=key)
        except ClientError:
            exists = False
        except (BotoCoreError, ValueError) as exc:
            raise NetworkError(f"verifica oggetto fallita: {exc}") from exc

        if exists:
            if conflict == "skip":
                return PutResult(path=f"s3://{bucket}/{key}", action="skipped")
            if conflict == "rename":
                key = self._next_free(client, bucket, key)
            action = "overwritten" if conflict == "overwrite" else "created"
        else:
            action = "created"

        try:
            client.put_object(Bucket=bucket, Key=key, Body=data)
        except (ClientError, BotoCoreError) as exc:
            raise NetworkError(f"upload S3 fallito su {bucket}/{key}: {exc}") from exc
        return PutResult(path=f"s3://{bucket}/{key}", action=action, size=len(data))

    @staticmethod
    def _next_free(client, bucket: str, key: str) -> str:  # type: ignore[no-untyped-def]
        if "/" in key:
            directory, _, name = key.rpartition("/")
            directory += "/"
        else:
            directory, name = "", key
        stem, dot, suffix = name.rpartition(".")
        if not dot:
            stem, suffix = name, ""
        counter = 1
        while True:
            if dot:
                candidate = f"{directory}{stem} ({counter}).{suffix}"
            else:
                candidate = f"{directory}{name} ({counter})"
            try:
                client.head_object(Bucket=bucket, Key=candidate)
            except ClientError:
                return candidate
            except (BotoCoreError, ValueError) as exc:
                raise NetworkError(f"verifica oggetto fallita: {exc}") from exc
            counter += 1
