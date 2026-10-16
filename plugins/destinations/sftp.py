"""DestinationSFTP — carica le immagini su un server SFTP remoto."""

from __future__ import annotations

import contextlib
from typing import ClassVar

import paramiko

from app.pathutil import sanitize_filename
from plugins.base import (
    AuthError,
    ConfigError,
    Destination,
    FieldSpec,
    NetworkError,
    PutResult,
    require,
)
from plugins.registry import register_destination


@register_destination
class DestinationSFTP(Destination):
    """Upload SFTP con creazione directory, gestione conflitti e timeout."""

    type: ClassVar[str] = "sftp"
    label: ClassVar[str] = "SFTP (remoto)"
    description: ClassVar[str] = "Copia i file su un server SFTP (NAS, fornitore, ecc.)."
    fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("host", "Host", required=True, placeholder="nas.interno.local"),
        FieldSpec("port", "Porta", type="number", default="22"),
        FieldSpec("username", "Username", required=True),
        FieldSpec("password", "Password", type="password", env_hint="${SFTP_PASSWORD}"),
        FieldSpec("key_file", "Chiave privata (path)", env_hint="${SFTP_KEY_FILE}"),
        FieldSpec("remote_dir", "Directory remota", required=True, default="/outgoing/images"),
        FieldSpec(
            "verify_host_key",
            "Verifica host key",
            type="checkbox",
            default=True,
        ),
        FieldSpec("timeout", "Timeout (secondi)", type="number", default="30"),
    )

    def _connect(self) -> paramiko.SSHClient:
        host = require(self.config, "host")
        username = require(self.config, "username")
        password = str(self.config.get("password") or "") or None
        key_file = str(self.config.get("key_file") or "") or None
        if not password and not key_file:
            raise ConfigError("indicare password o chiave privata per SFTP")
        try:
            port = int(self.config.get("port") or 22)
        except (TypeError, ValueError) as exc:
            raise ConfigError("porta SFTP non valida") from exc

        client = paramiko.SSHClient()
        if self.config.get("verify_host_key", True):
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                port=port,
                username=username,
                password=password,
                key_filename=key_file,
                timeout=self.timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.AuthenticationException as exc:
            raise AuthError(f"autenticazione SFTP fallita su {host}") from exc
        except (TimeoutError, OSError, paramiko.SSHException) as exc:
            raise NetworkError(f"connessione SFTP fallita su {host}:{port} - {exc}") from exc
        return client

    @staticmethod
    def _mkdir_p(sftp: paramiko.SFTPClient, path: str) -> None:
        parts = [p for p in path.strip("/").split("/") if p]
        current = ""
        for part in parts:
            current += "/" + part
            try:
                sftp.stat(current)
            except OSError:
                try:
                    sftp.mkdir(current)
                except OSError as exc:
                    raise ConfigError(f"impossibile creare la directory {current}: {exc}") from exc

    def put(self, filename: str, data: bytes, conflict: str = "rename") -> PutResult:
        name = sanitize_filename(filename, default="immagine.jpg")
        remote_dir = require(self.config, "remote_dir", "Directory remota").rstrip("/") or "/"
        client = self._connect()
        try:
            try:
                sftp = client.open_sftp()
            except paramiko.SSHException as exc:
                raise NetworkError(f"canale SFTP non disponibile: {exc}") from exc
            try:
                self._mkdir_p(sftp, remote_dir)
                remote_path = f"{remote_dir}/{name}"
                exists = True
                try:
                    sftp.stat(remote_path)
                except OSError:
                    exists = False
                if exists:
                    if conflict == "skip":
                        return PutResult(path=remote_path, action="skipped")
                    if conflict == "rename":
                        remote_path = self._next_free(sftp, remote_dir, name)
                    elif conflict == "overwrite":
                        with contextlib.suppress(OSError):
                            sftp.remove(remote_path)
                with sftp.open(remote_path, "wb") as handle:
                    handle.write(data)
            except OSError as exc:
                raise NetworkError(f"upload SFTP fallito verso {remote_path}: {exc}") from exc
            finally:
                sftp.close()
        finally:
            client.close()
        action = "overwritten" if conflict == "overwrite" and exists else "created"
        return PutResult(path=remote_path, action=action, size=len(data))

    @staticmethod
    def _next_free(sftp: paramiko.SFTPClient, directory: str, name: str) -> str:
        stem, dot, suffix = name.rpartition(".")
        if not dot:
            stem, suffix = name, ""
        counter = 1
        while True:
            candidate_name = (
                f"{stem} ({counter}).{suffix}" if dot else f"{name} ({counter})"
            )
            candidate = f"{directory}/{candidate_name}"
            try:
                sftp.stat(candidate)
            except OSError:
                return candidate
            counter += 1
