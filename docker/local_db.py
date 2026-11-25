"""Prepare only MySQL's local Docker secrets and the schema for the configured database."""

import os
import secrets
from pathlib import Path

from app.core.config import get_settings

settings = get_settings()
target = Path("/db-config")
for name, value in (
    ("database", settings.mysql_database),
    ("user", settings.mysql_user),
    ("password", settings.mysql_password),
):
    path = target / name
    path.write_text(value)
    os.chown(path, 999, 999)
    path.chmod(0o400)
root_key = target / "root-password"
if not root_key.exists():
    root_key.write_text(secrets.token_urlsafe(48))
    os.chown(root_key, 999, 999)
    root_key.chmod(0o400)

# The image creates MYSQL_DATABASE and grants the configured application user access.
# Install the table definitions there, including when its name differs from 'harei'.
schema = Path("/schema-source.sql").read_text()
upgrades = Path("/schema-upgrades")
parts = [
    schema[schema.index("CREATE TABLE") :],
    (upgrades / "002_binding_names_and_address_refill.sql").read_text(),
    (upgrades / "003_member_address_autofill.sql").read_text(),
    (upgrades / "004_returned_address.sql").read_text(),
    (upgrades / "006_live_gift_eligibility.sql").read_text(),
    (upgrades / "007_captain_startup_backfill.sql").read_text(),
]
(target / "schema.sql").write_text("\n".join(parts))
