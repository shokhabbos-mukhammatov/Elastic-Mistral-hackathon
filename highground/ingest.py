"""CLI: python -m highground.ingest setup | layers | places | backfill [years] | poll [feed..] | profiles | guides | replay DATE"""

import sys
import time
from datetime import UTC, datetime, timedelta

from highground import feeds, layers, store


def main(argv: list[str]) -> None:
    s, http = store.Store(), feeds.client()
    cmd, args = argv[0], argv[1:]
    t0 = time.time()
    if cmd == "setup":
        print(store.setup(s))
    elif cmd == "layers":
        print("zones", s.write("zones", layers.zone_docs(http), refresh=True))
        store.enrich_pipeline(s)
        print("enrich pipeline ready")
    elif cmd == "places":
        print("places", s.write("places", layers.place_docs(http, addresses="--no-addresses" not in args)))
    elif cmd == "backfill":
        years = float(args[0]) if args else 5
        start = datetime.now(UTC) - timedelta(days=365 * years)
        batch, total = [], 0
        for d in feeds.fetch_datasf(http, start, services=("Sewer Issues", "Sewer", "Catch Basin Maintenance", "Tree Maintenance")):
            batch.append(d)
            if len(batch) >= 5000:
                total += s.write("reports", batch, pipeline=True, merge_fn=feeds.merge)
                batch = []
                print("backfill", total, flush=True)
        total += s.write("reports", batch, pipeline=True, merge_fn=feeds.merge)
        print("backfill done", total)
    elif cmd == "poll":
        for name in args or list(feeds.FEEDS):
            print(store.run_feed(s, name, http))
    elif cmd == "profiles":
        from highground import replay

        print("profiles", replay.build_profiles(s, datetime.now(UTC), index="profiles"))
    elif cmd == "guides":
        from highground import guides

        print("guides", guides.load(s, http))
    elif cmd == "replay":
        from highground import replay

        print(replay.build_dataset(s, http, args[0] if args else "2022-12-31"))
    else:
        raise SystemExit(__doc__)
    print(f"{cmd}: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main(sys.argv[1:] or ["help"])
