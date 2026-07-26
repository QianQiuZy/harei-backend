from alembic import op
import sqlalchemy as sa


revision = "20260726_audit_repair"
down_revision = "20260725_music"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "music_catalog_revision" not in tables:
        _ = op.create_table(
            "music_catalog_revision",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
            sa.Column(
                "updated_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.text(
                    "CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
                ),
            ),
        )
    op.execute(
        "INSERT INTO music_catalog_revision (id, revision) "
        + "SELECT 1, 0 WHERE NOT EXISTS "
        + "(SELECT 1 FROM music_catalog_revision WHERE id = 1)"
    )

    if "music_audit_events" not in tables:
        _ = op.create_table(
            "music_audit_events",
            sa.Column("audit_id", sa.Integer, primary_key=True),
            sa.Column("actor", sa.String(255), nullable=False),
            sa.Column("action", sa.String(80), nullable=False),
            sa.Column("entity_type", sa.String(40), nullable=False),
            sa.Column("entity_id", sa.String(100), nullable=False),
            sa.Column("details", sa.JSON, nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )
        op.create_index(
            "ix_audit_action_entity_created",
            "music_audit_events",
            ["action", "entity_type", "entity_id", "created_at"],
        )


def downgrade() -> None:
    pass
