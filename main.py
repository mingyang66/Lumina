import argparse
import os
import sys

from lumina.application import LuminaApp
from lumina.settings import load_config
from lumina.database import Database


def cmd_run(cfg, config_path=None):
    LuminaApp(cfg, config_path).start()


def _print_rows(rows):
    if not rows:
        print("(empty)")
        return
    print(f"{'ID':>6}  {'':1}{'CATEGORY':<8} {'SIZE':>9}  {'TAGS':<16} "
          f"{'SOURCE':<20} {'TIME':<19} PREVIEW")
    for r in rows:
        size = f"{r['size'] // 1024} KB" if r["size"] else "0"
        preview = (r["preview"] or "").strip()
        pin = "*" if r["pinned"] else ""
        cat = r["category"] or r["kind"]
        print(f"{r['id']:>6}  {pin:1}{cat:<8} {size:>9}  "
              f"{(r['tags'] or '')[:16]:<16} {(r['source'] or '')[:20]:<20} "
              f"{r['created_at'][:19]:<19} {preview}")


def _open_db(cfg):
    db = Database(cfg["db_path"])
    db.init_schema()
    return db


def cmd_list(cfg, n):
    db = _open_db(cfg)
    try:
        _print_rows(db.list_recent(n))
    finally:
        db.close()


def cmd_search(cfg, query, n):
    db = _open_db(cfg)
    try:
        _print_rows(db.search(query, n))
    finally:
        db.close()


def cmd_pin(cfg, clip_id, pinned):
    db = _open_db(cfg)
    try:
        if not db.set_pinned(clip_id, pinned):
            print(f"record {clip_id} not found")
            sys.exit(1)
        print(f"#{clip_id} {'pinned' if pinned else 'unpinned'}")
    finally:
        db.close()


def cmd_tag(cfg, clip_id, tags):
    db = _open_db(cfg)
    try:
        if not db.set_tags(clip_id, tags):
            print(f"record {clip_id} not found")
            sys.exit(1)
        row = db.get(clip_id)
        print(f"#{clip_id} tags: {row['tags'] or '(cleared)'}")
    finally:
        db.close()


def cmd_export(cfg, clip_id, out):
    db = _open_db(cfg)
    try:
        row = db.get(clip_id)
        if not row:
            print(f"record {clip_id} not found")
            sys.exit(1)
        if row["kind"] == "image":
            out = out or f"clip_{clip_id}.png"
            with open(out, "wb") as f:
                f.write(row["data"])
        elif row["category"] == "file" and row["data"]:
            out = out or os.path.basename(row["content"] or "") or f"file_{clip_id}"
            with open(out, "wb") as f:
                f.write(row["data"])
        else:
            out = out or f"clip_{clip_id}.txt"
            with open(out, "w", encoding="utf-8") as f:
                f.write(row["content"] or "")
        print(f"exported -> {os.path.abspath(out)}")
    finally:
        db.close()


def cmd_clean(cfg):
    db = _open_db(cfg)
    try:
        deleted = db.cleanup(cfg.get("retention_days", 30), cfg.get("max_rows", 5000))
        print(f"removed {deleted} expired record(s) (pinned records are kept)")
    finally:
        db.close()


def cmd_stats(cfg):
    db = _open_db(cfg)
    try:
        rows, size, pinned = db.stats()
        print(f"db: {os.path.abspath(db.path)} ({size / 1024 / 1024:.2f} MB)")
        print(f"fts search: {'enabled' if db._fts else 'disabled (using LIKE)'}")
        if not rows:
            print("(empty)")
            return
        for r in rows:
            print(f"  {r['cat']:<8} {r['cnt']:>6} record(s)  "
                  f"oldest: {r['oldest'][:19]}  newest: {r['newest'][:19]}")
        print(f"  pinned {pinned:>6} record(s)")
    finally:
        db.close()


def main():
    # pythonw.exe has no standard streams; keep background startup from failing
    # when the application logs status messages.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    parser = argparse.ArgumentParser(prog="lumina",
                                     description="clipboard & screenshot archiver (SQLite)")
    default_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    parser.add_argument("-c", "--config", default=default_config, help="config file path")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="start monitoring (default)")
    p_list = sub.add_parser("list", help="list recent records")
    p_list.add_argument("-n", type=int, default=20)
    p_search = sub.add_parser("search", help="full-text search (content/tags/source)")
    p_search.add_argument("query")
    p_search.add_argument("-n", type=int, default=20)
    p_pin = sub.add_parser("pin", help="pin a record (exempt from cleanup)")
    p_pin.add_argument("id", type=int)
    p_unpin = sub.add_parser("unpin", help="unpin a record")
    p_unpin.add_argument("id", type=int)
    p_tag = sub.add_parser("tag", help="set tags (comma separated, empty to clear)")
    p_tag.add_argument("id", type=int)
    p_tag.add_argument("tags", nargs="*", default=[])
    p_export = sub.add_parser("export", help="export a record to file")
    p_export.add_argument("id", type=int)
    p_export.add_argument("out", nargs="?")
    sub.add_parser("clean", help="run retention cleanup now")
    sub.add_parser("stats", help="show database statistics")
    args = parser.parse_args()

    cfg = load_config(args.config)
    cmd = args.cmd or "run"
    if cmd == "run":
        cmd_run(cfg, args.config)
    elif cmd == "list":
        cmd_list(cfg, args.n)
    elif cmd == "search":
        cmd_search(cfg, args.query, args.n)
    elif cmd == "pin":
        cmd_pin(cfg, args.id, True)
    elif cmd == "unpin":
        cmd_pin(cfg, args.id, False)
    elif cmd == "tag":
        cmd_tag(cfg, args.id, ",".join(args.tags))
    elif cmd == "export":
        cmd_export(cfg, args.id, args.out)
    elif cmd == "clean":
        cmd_clean(cfg)
    elif cmd == "stats":
        cmd_stats(cfg)


if __name__ == "__main__":
    main()
