"""SourceSFTP — cerca e legge immagini da una directory remota via SFTP.

Sorgente dimostrativa per il caso "directory remota" (backup, NAS, fornitori).
Le credenziali arrivano da variabili d'ambiente tramite ``${VAR}``.
"""

from __future__ import annotations

import fnmatch
from typing import ClassVar

import paramiko

from plugins.base import (
    AuthError,
    ConfigError,
    FetchResult,
    FieldSpec,
    ImageSource,
    NetworkError,
    NotFoundError,
    TooLargeError,
    format_template,
    require,
)
from plugins.registry import register_source

DEFAULT_PATTERNS = "{codice}.jpg, {codice}.png, {codice}_*.*"


@register_source
class SourceSFTP(ImageSource):
    """Sorgente remota SFTP con pattern, timeout e connessione riusata per riga."""

    type: ClassVar[str] = "sftp"
    label: ClassVar[str] = "SFTP (remoto)"
    description: ClassVar[str] = (
        "Elenco e lettura di file su un server SFTP; le credenziali possono "
        "arrivare da ${SFTP_PASSWORD} / ${SFTP_KEY_FILE} definiti nel .env."
    )
    fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("host", "Host", required=True, placeholder="nas.interno.local"),
        FieldSpec("port", "Porta", type="number", default="22"),
        FieldSpec("username", "Username", required=True),
        FieldSpec("password", "Password", type="password", env_hint="${SFTP_PASSWORD}"),
        FieldSpec("key_file", "Chiave privata (path)", env_hint="${SFTP_KEY_FILE}"),
        FieldSpec("remote_dir", "Directory remota", required=True, default="/images"),
        FieldSpec(
            "patterns",
            "Pattern (separati da virgola)",
            type="textarea",
            default=DEFAULT_PATTERNS,
        ),
        FieldSpec(
            "verify_host_key",
            "Verifica host key",
            type="checkbox",
            default=True,
            help="Disattivare solo per ambienti interni di test.",
        ),
        FieldSpec("timeout", "Timeout (secondi)", type="number", default="15"),
    )

    def _patterns(self) -> list[str]:
        raw = str(self.config.get("patterns") or DEFAULT_PATTERNS)
        patterns = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
        if not patterns:
            raise ConfigError("almeno un pattern è obbligatorio")
        return patterns

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

    def fetch(self, product_code: str) -> FetchResult:
        code = str(product_code or "").strip()
        if not code:
            raise NotFoundError("codice prodotto vuoto")
        remote_dir = require(self.config, "remote_dir", "Directory remota").rstrip("/") or "/"
        self.acquire_slot()

        client = self._connect()
        try:
            try:
                sftp = client.open_sftp()
            except paramiko.SSHException as exc:
                raise NetworkError(f"canale SFTP non disponibile: {exc}") from exc
            try:
                try:
                    entries = sftp.listdir(remote_dir)
                except FileNotFoundError as exc:
                    raise ConfigError(f"directory remota inesistente: {remote_dir}") from exc
                except OSError as exc:
                    raise NetworkError(f"lettura directory remota fallita: {exc}") from exc

                matched = self._match(entries, code)
                if not matched:
                    raise NotFoundError(
                        f"nessun file per il codice {code!r} in {remote_dir} "
                        f"(pattern: {', '.join(self._patterns())})"
                    )
                remote_path = f"{remote_dir}/{matched}"
                try:
                    stat = sftp.stat(remote_path)
                except OSError as exc:
                    raise NetworkError(f"stat fallito su {remote_path}: {exc}") from exc
                if stat.st_size > self.max_bytes:
                    raise TooLargeError(f"file oltre {self.max_bytes} byte: {matched}")
                with sftp.open(remote_path, "rb") as handle:
                    content = handle.read(self.max_bytes + 1)
                if len(content) > self.max_bytes:
                    raise TooLargeError(f"file oltre {self.max_bytes} byte: {matched}")
            finally:
                sftp.close()
        finally:
            client.close()

        return FetchResult(
            location=remote_path,
            content=content,
            meta={"size": len(content), "host": self.config.get("host")},
        )

    def _match(self, entries: list[str], code: str) -> str | None:
        for raw_pattern in self._patterns():
            pattern = format_template(
                raw_pattern, {"codice": code, "code": code}, context="patterns"
            )
            hits = sorted(
                e
                for e in entries
                if fnmatch.fnmatch(e, pattern) and "/" not in e and e not in (".", "..")
            )
            if hits:
                return hits[0]
        return None
