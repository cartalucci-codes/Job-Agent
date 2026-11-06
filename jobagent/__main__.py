"""Run with:  python -m jobagent   (add --demo to start with sample data)"""
import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="jobagent")
    parser.add_argument("--demo", action="store_true", help="fill an empty database with sample data")
    args = parser.parse_args()

    from . import config, db
    cfg = config.load_config()
    db.init()
    if args.demo:
        from . import demo
        demo.seed()

    host, port = cfg["server"]["host"], int(cfg["server"]["port"])
    if host not in ("127.0.0.1", "localhost", "::1") and os.environ.get("JOBAGENT_ALLOW_REMOTE") != "1":
        sys.exit(f"Refusing to listen on {host}: this app has one shared login and must stay on your own computer.\n"
                 "Set server.host back to 127.0.0.1 (or JOBAGENT_ALLOW_REMOTE=1 if you really know what you are doing).")

    import uvicorn
    uvicorn.run("jobagent.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
