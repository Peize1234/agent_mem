from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from auto_tuner_models import ConversationTurn, ExtractedHistory, ExtractedSession, RawMessage

_SCOPE_FIELD = re.compile(r"(?:^|&)(agent_id|run_id|user_id)=")
_REQUIRED_MESSAGE_COLUMNS = {
    "id",
    "session_scope",
    "role",
    "content",
    "created_at",
    "turn_index",
    "status",
}


def parse_session_scope(session_scope: str) -> dict[str, str]:
    """Parse the deterministic ``_build_session_scope()`` representation."""

    matches = list(_SCOPE_FIELD.finditer(str(session_scope or "")))
    values: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(session_scope)
        values[match.group(1)] = session_scope[match.end() : end]
    return values


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _read_only_connection(path: Path) -> sqlite3.Connection:
    encoded = quote(str(path.resolve()), safe="/")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _validate_messages_schema(connection: sqlite3.Connection) -> None:
    table = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages'").fetchone()
    if table is None:
        raise ValueError("history database has no messages table")
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(messages)")}
    missing = sorted(_REQUIRED_MESSAGE_COLUMNS - columns)
    if missing:
        raise ValueError(f"messages table is missing required columns: {', '.join(missing)}")


def _message_rows(connection: sqlite3.Connection, user_id: str) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    escaped_user_id = _escape_like(user_id)
    exact = f"user_id={user_id}"
    return connection.execute(
        """
        SELECT rowid, id, session_scope, role, content, name, created_at, turn_index, status
        FROM messages
        WHERE role IN ('user', 'assistant')
          AND session_scope IS NOT NULL
          AND (
              session_scope = ?
              OR session_scope LIKE ? ESCAPE '\\'
              OR session_scope LIKE ? ESCAPE '\\'
              OR session_scope LIKE ? ESCAPE '\\'
          )
        ORDER BY session_scope ASC, turn_index ASC, DATETIME(created_at) ASC, rowid ASC
        """,
        (
            exact,
            f"user_id={escaped_user_id}&%",
            f"%&user_id={escaped_user_id}",
            f"%&user_id={escaped_user_id}&%",
        ),
    ).fetchall()


def extract_user_history(
    *,
    user_id: str,
    history_db_path: str | Path,
    output_dir: str | Path | None = None,
) -> ExtractedHistory:
    """Extract complete raw QA turns for exactly one user from ``messages``."""

    normalized_user_id = str(user_id).strip()
    if not normalized_user_id:
        raise ValueError("user_id is required")
    database_path = Path(history_db_path).expanduser().resolve()
    if not database_path.is_file():
        raise FileNotFoundError(f"history database does not exist: {database_path}")

    connection = _read_only_connection(database_path)
    try:
        connection.execute("BEGIN")
        _validate_messages_schema(connection)
        rows = _message_rows(connection, normalized_user_id)
        connection.rollback()
    finally:
        connection.close()

    rows_by_session: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        scope = str(row["session_scope"] or "")
        if parse_session_scope(scope).get("user_id") == normalized_user_id:
            rows_by_session[scope].append(row)

    sessions: list[ExtractedSession] = []
    incomplete_turn_count = 0
    for session_scope, session_rows in rows_by_session.items():
        scope = parse_session_scope(session_scope)
        rows_by_turn: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for row in session_rows:
            rows_by_turn[int(row["turn_index"] or 0)].append(row)
        turns: list[ConversationTurn] = []
        for turn_index in sorted(rows_by_turn):
            raw_messages = tuple(
                RawMessage(
                    rowid=int(row["rowid"]),
                    message_id=str(row["id"] or ""),
                    role=str(row["role"]),
                    content=str(row["content"] or ""),
                    name=str(row["name"]) if row["name"] is not None else None,
                    created_at=str(row["created_at"]) if row["created_at"] is not None else None,
                    turn_index=turn_index,
                    status=str(row["status"] or "active"),
                )
                for row in rows_by_turn[turn_index]
            )
            user_messages = [message.content for message in raw_messages if message.role == "user"]
            assistant_messages = [message.content for message in raw_messages if message.role == "assistant"]
            if not user_messages or not assistant_messages:
                incomplete_turn_count += 1
                continue
            turns.append(
                ConversationTurn(
                    source_turn_index=turn_index,
                    messages=raw_messages,
                    question="\n".join(user_messages),
                    answer="\n".join(assistant_messages),
                )
            )
        if turns:
            sessions.append(
                ExtractedSession(
                    session_scope=session_scope,
                    user_id=normalized_user_id,
                    run_id=scope.get("run_id"),
                    agent_id=scope.get("agent_id"),
                    turns=tuple(turns),
                )
            )

    sessions.sort(
        key=lambda session: (
            min((message.created_at or "", message.rowid) for turn in session.turns for message in turn.messages),
            session.session_scope,
        )
    )
    history = ExtractedHistory(
        user_id=normalized_user_id,
        history_db_path=str(database_path),
        sessions=tuple(sessions),
        incomplete_turn_count=incomplete_turn_count,
    )
    if output_dir is not None:
        destination = Path(output_dir).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "extracted_history.json").write_text(
            json.dumps(history.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return history


def _default_history_db_path() -> str:
    from mem0.configs.production import load_production_memory_config

    return load_production_memory_config(resolve_environment=False).history_db_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract one user's raw conversation history")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--history-db-path", default=_default_history_db_path())
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    history = extract_user_history(
        user_id=args.user_id,
        history_db_path=args.history_db_path,
        output_dir=args.output_dir,
    )
    print(json.dumps({"sessions": len(history.sessions), "qa_turns": history.qa_turn_count}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
