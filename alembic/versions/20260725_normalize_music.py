from alembic import op
import sqlalchemy as sa


revision = "20260725_music"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "songs" not in tables:
        _ = op.create_table(
            "songs",
            sa.Column("song_id", sa.Integer, primary_key=True),
            sa.Column("source_key", sa.String(80), nullable=False, unique=True),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("artist", sa.String(500), nullable=False),
            sa.Column("artists", sa.JSON, nullable=False),
            sa.Column("genre", sa.String(100), nullable=False),
            sa.Column("language", sa.String(50), nullable=False),
            sa.Column("work_type", sa.String(50), nullable=False),
            sa.Column("notes", sa.Text, nullable=False),
            sa.Column(
                "metadata_status",
                sa.String(30),
                nullable=False,
                server_default="complete",
            ),
            sa.Column(
                "status", sa.String(20), nullable=False, server_default="active"
            ),
            sa.Column("version", sa.Integer, nullable=False, server_default="1"),
            sa.Column(
                "created_at", sa.DateTime, server_default=sa.text("CURRENT_TIMESTAMP")
            ),
            sa.Column(
                "updated_at",
                sa.DateTime,
                server_default=sa.text(
                    "CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
                ),
            ),
        )
        op.create_index("ix_songs_title", "songs", ["title"])
        op.create_index(
            "ix_songs_filters",
            "songs",
            ["status", "genre", "language", "work_type"],
        )

    if "song_performances" not in tables:
        _ = op.create_table(
            "song_performances",
            sa.Column("performance_id", sa.Integer, primary_key=True),
            sa.Column("source_key", sa.String(80), nullable=False, unique=True),
            sa.Column(
                "song_id",
                sa.Integer,
                sa.ForeignKey("songs.song_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("performed_on", sa.Date, nullable=False),
            sa.Column("platform", sa.String(100), nullable=False),
            sa.Column("stream_id", sa.String(100)),
            sa.Column("stream_title", sa.String(255)),
            sa.Column("stream_url", sa.String(2048)),
            sa.Column("clip_url", sa.String(2048)),
            sa.Column(
                "created_at", sa.DateTime, server_default=sa.text("CURRENT_TIMESTAMP")
            ),
            sa.Column(
                "updated_at",
                sa.DateTime,
                server_default=sa.text(
                    "CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
                ),
            ),
        )
        op.create_index(
            "ix_performances_song_date",
            "song_performances",
            ["song_id", "performed_on"],
        )

    if "music_catalog_revision" not in tables:
        _ = op.create_table(
            "music_catalog_revision",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("revision", sa.Integer, nullable=False),
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
                "created_at", sa.DateTime, server_default=sa.text("CURRENT_TIMESTAMP")
            ),
        )
        op.create_index(
            "ix_audit_action_entity_created",
            "music_audit_events",
            ["action", "entity_type", "entity_id", "created_at"],
        )


def downgrade() -> None:
    op.drop_table("music_audit_events")
    op.drop_table("music_catalog_revision")
    op.drop_table("song_performances")
    op.drop_table("songs")
