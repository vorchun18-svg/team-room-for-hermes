"""Замер бюджетов тела карточки (WAVE25 §M2): «до» и «после», только чтение.

Два режима:

* ``--fixtures`` — собрать тела карточек НЫНЕШНИМ кодом (`contract`) на
  вымышленных данных и напечатать таблицу «тип / знаков / потолок»;
* ``--board <kanban.db>`` — прочитать ЖИВУЮ доску **только на чтение**
  (`sqlite mode=ro`) и показать размеры тел по типам: так видно, что было
  «до» правки (карточки созданы старым кодом) и сколько карточек сверх бюджета.

Ничего не пишет: ни в доску, ни в профили Hermes.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import card_budget, contract  # noqa: E402


class _Cfg:
    def __init__(self, base: Path):
        self.vault_dir = base / "Vault" / "Rooms"
        self.reports_dir = base / "data" / "reports"
        self.reportkit_dir = base / "reportkit"


ROOM = {"id": "studio", "title": "Сайт студии", "board": "team", "tenant": "room:studio",
        "orchestrator": "lead", "critic": "critic",
        "members": [{"profile": "designer", "role": "worker", "note": "собирает макеты"}],
        "context_files": [], "critic_pass": True}
TEXT = ("Проверь сайт студии и скажи, что делать по шагам: сроки, разделы, кто отвечает. " * 12)
SVOD = ("## Коротко\nСвод по сайту готов.\n\n## Что сделано\n"
        + "\n".join("- пункт %d: проверили раздел и поправили текст" % i for i in range(300)))
DIGEST = {"version": 12, "count": 4,
          "text": "Ответы Михаила на вопросы — 4:\n"
                  + "\n".join("%d. Вопрос: вопрос %d про сроки\n   Ответ: да" % (i, i)
                              for i in range(1, 5))}


def build_fixtures() -> list[dict]:
    """Тела карточек нынешним кодом — то, что уйдёт в доску после правки."""
    with tempfile.TemporaryDirectory(prefix="tr-budgets-") as tmp:
        cfg = _Cfg(Path(tmp))
        rows = [
            {"kind": "leader", "name": "корневой запрос",
             "body": contract.build_request_body(ROOM, TEXT, "request", cfg)},
            {"kind": "leader", "name": "бриф",
             "body": contract.build_brief_body(ROOM, TEXT[:600], "brief", cfg)},
            {"kind": "svod", "name": "доработка свода",
             "body": contract.build_revision_body(ROOM, TEXT[:900], SVOD, None, "короче", cfg,
                                                  digest=DIGEST, prev_card="t_prev123")},
            {"kind": "critic", "name": "критик",
             "body": contract.build_critic_body(ROOM, TEXT[:900], SVOD, cfg)},
            {"kind": "svod", "name": "доработка без прошлой версии",
             "body": contract.build_revision_body(ROOM, TEXT[:900], "", None, "короче", cfg,
                                                  digest=DIGEST)},
        ]
        for row in rows:
            row["chars"] = len(row["body"])
        return rows


def read_board(path: Path, limit: int = 400) -> list[dict]:
    """Размеры тел карточек живой доски (только чтение)."""
    db = Path(path)
    if not db.exists():
        raise SystemExit("нет файла доски: %s" % db)
    con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT id, title, status, length(body) AS chars "
                           "FROM tasks ORDER BY created_at DESC LIMIT ?",
                           (int(limit),)).fetchall()
    finally:
        con.close()
    out = []
    for row in rows:
        chars = int(row["chars"] or 0)
        title = str(row["title"] or "")
        out.append({"id": str(row["id"]), "title": title, "status": str(row["status"] or ""),
                    "chars": chars, "kind": card_budget.kind_of("", title)})
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Замер бюджетов тела карточки (W25-M2)")
    parser.add_argument("--board", default="", help="путь к kanban.db (только чтение)")
    parser.add_argument("--fixtures", action="store_true", help="замерить тела нынешним кодом")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--limit", type=int, default=400)
    args = parser.parse_args(argv)

    report: dict = {"budgets": card_budget.BUDGETS}
    if args.fixtures or not args.board:
        rows = build_fixtures()
        report["fixtures"] = [{k: r[k] for k in ("kind", "name", "chars")} | {
            "limit": card_budget.limit(r["kind"])} for r in rows]
        report["fixtures_audit"] = card_budget.audit(rows)
    if args.board:
        rows = read_board(Path(args.board), args.limit)
        report["board"] = {"path": str(args.board), "cards": len(rows)}
        report["board_audit"] = card_budget.audit(rows)
        report["board_top"] = sorted(rows, key=lambda r: -r["chars"])[:10]

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if "fixtures" in report:
        print("ТЕЛА КАРТОЧЕК, СОБРАННЫЕ НЫНЕШНИМ КОДОМ")
        print("%-26s %-12s %8s %8s %6s" % ("что", "тип", "знаков", "потолок", "итог"))
        for row in report["fixtures"]:
            limit = card_budget.limit(row["kind"])
            print("%-26s %-12s %8d %8d %6s" % (row["name"][:26], row["kind"], row["chars"], limit,
                                               "ок" if row["chars"] <= limit else "СВЕРХ"))
    if "board" in report:
        audit = report["board_audit"]
        print("\nЖИВАЯ ДОСКА (только чтение): %s" % report["board"]["path"])
        print("карточек: %d, сверх бюджета: %d" % (audit["total"], audit["over"]))
        print("%-14s %7s %6s %8s %8s" % ("тип", "карточек", "сверх", "макс", "потолок"))
        for kind, item in sorted(audit["kinds"].items()):
            print("%-14s %7d %6d %8d %8d" % (kind, item["count"], item["over"], item["max"],
                                             item["limit"]))
        print("\nсамые крупные:")
        for row in report["board_top"]:
            print("  %8d  %-12s %s %s" % (row["chars"], row["kind"], row["id"], row["title"][:50]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
