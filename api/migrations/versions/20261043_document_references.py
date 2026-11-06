"""Index document sources without changing the legacy JSON payloads."""

from alembic import op
import sqlalchemy as sa


revision = "20261043_document_references"
down_revision = "20261042_project_templates"
branch_labels = None
depends_on = None

SOURCES = (("analysis_runs", "source_ids", "analysis"),
           ("harness_tasks", "source_document_ids", "harness_task"))


def upgrade() -> None:
    op.create_table(
        "document_references",
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("document_id", "source_kind", "source_id"),
    )
    op.create_index("ix_document_references_source", "document_references", ["source_kind", "source_id"])
    dialect = op.get_bind().dialect.name
    available = [(table, field, kind) for table, field, kind in SOURCES
                 if field in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}]
    for table, field, kind in available:
        if dialect == "sqlite":
            array = (f"CASE WHEN json_valid({table}.{field}) THEN "
                     f"CASE WHEN json_type({table}.{field}) = 'array' THEN {table}.{field} ELSE '[]' END "
                     f"ELSE '[]' END")
            op.execute(sa.text(f"""
                INSERT OR IGNORE INTO document_references(document_id,source_kind,source_id)
                SELECT DISTINCT item.value, '{kind}', {table}.id FROM {table}, json_each({array}) AS item
                WHERE item.type = 'text'
            """))
            new_array = (f"CASE WHEN json_valid(NEW.{field}) THEN "
                         f"CASE WHEN json_type(NEW.{field}) = 'array' THEN NEW.{field} ELSE '[]' END "
                         f"ELSE '[]' END")
            for operation in ("insert", "update"):
                cleanup = (f"DELETE FROM document_references WHERE source_kind = '{kind}' AND source_id = OLD.id;"
                           if operation == "update" else "")
                op.execute(sa.text(f"""
                    CREATE TRIGGER ta_document_refs_{kind}_{operation}
                    AFTER {operation.upper()}{f' OF id, {field}' if operation == 'update' else ''} ON {table}
                    BEGIN
                        {cleanup}
                        INSERT OR IGNORE INTO document_references(document_id,source_kind,source_id)
                        SELECT DISTINCT item.value, '{kind}', NEW.id FROM json_each({new_array}) AS item
                        WHERE item.type = 'text';
                    END
                """))
            op.execute(sa.text(f"""
                CREATE TRIGGER ta_document_refs_{kind}_delete AFTER DELETE ON {table}
                BEGIN DELETE FROM document_references WHERE source_kind = '{kind}' AND source_id = OLD.id; END
            """))
        elif dialect == "postgresql":
            op.execute(sa.text(f"""
                INSERT INTO document_references(document_id,source_kind,source_id)
                SELECT DISTINCT item.value #>> '{{}}', '{kind}', {table}.id
                FROM {table}, jsonb_array_elements(
                    CASE WHEN jsonb_typeof({table}.{field}::jsonb) = 'array'
                         THEN {table}.{field}::jsonb ELSE '[]'::jsonb END) AS item(value)
                WHERE jsonb_typeof(item.value) = 'string' ON CONFLICT DO NOTHING
            """))
        else:
            raise RuntimeError(f"Unsupported database for document references: {dialect}")

    if dialect == "postgresql":
        op.execute(sa.text("""
            CREATE FUNCTION ta_sync_document_references() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE source_type text; values_json jsonb; source_key text;
            BEGIN
                source_type := CASE TG_TABLE_NAME WHEN 'analysis_runs' THEN 'analysis'
                               ELSE 'harness_task' END;
                IF TG_OP <> 'INSERT' THEN
                    DELETE FROM document_references
                    WHERE source_kind = source_type AND source_id = OLD.id;
                END IF;
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                values_json := CASE WHEN TG_TABLE_NAME = 'analysis_runs' THEN NEW.source_ids::jsonb
                                    ELSE NEW.source_document_ids::jsonb END;
                FOR source_key IN SELECT value #>> '{}' FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(values_json) = 'array' THEN values_json ELSE '[]'::jsonb END
                ) AS item(value) WHERE jsonb_typeof(value) = 'string' LOOP
                    INSERT INTO document_references(document_id,source_kind,source_id)
                    VALUES (source_key,source_type,NEW.id) ON CONFLICT DO NOTHING;
                END LOOP;
                RETURN NEW;
            END;
            $$
        """))
        for table, field, kind in available:
            op.execute(sa.text(f"""
                CREATE TRIGGER ta_document_refs_{kind}_insert AFTER INSERT ON {table}
                FOR EACH ROW EXECUTE FUNCTION ta_sync_document_references()
            """))
            op.execute(sa.text(f"""
                CREATE TRIGGER ta_document_refs_{kind}_update AFTER UPDATE OF id, {field} ON {table}
                FOR EACH ROW EXECUTE FUNCTION ta_sync_document_references()
            """))
            op.execute(sa.text(f"""
                CREATE TRIGGER ta_document_refs_{kind}_delete AFTER DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION ta_sync_document_references()
            """))


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    for table, _, kind in SOURCES:
        for operation in ("insert", "update", "delete"):
            if dialect == "postgresql":
                op.execute(sa.text(f"DROP TRIGGER IF EXISTS ta_document_refs_{kind}_{operation} ON {table}"))
            elif dialect == "sqlite":
                op.execute(sa.text(f"DROP TRIGGER IF EXISTS ta_document_refs_{kind}_{operation}"))
            else:
                raise RuntimeError(f"Unsupported database for document references: {dialect}")
    if dialect == "postgresql":
        op.execute(sa.text("DROP FUNCTION ta_sync_document_references()"))
    op.drop_index("ix_document_references_source", table_name="document_references")
    op.drop_table("document_references")
