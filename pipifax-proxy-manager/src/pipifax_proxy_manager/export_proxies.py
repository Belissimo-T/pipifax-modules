import argparse
import csv
from pathlib import Path

from . import ProxyProvider, Proxies


def main():
    argparser = argparse.ArgumentParser()
    argparser.add_argument("proxies_json_file", type=str, default="proxies.json", nargs="?")
    argparser.add_argument("output_file", type=str)
    argparser.add_argument("--output_type", type=str, default="csv", choices=["csv", "txt"])
    args = argparser.parse_args()

    proxies = Proxies.deserialize(Path(args.proxies_json_file).read_bytes()).proxies

    if args.output_type == "csv":
        # Extract column headers dynamically
        sample_proxy_data = next(iter(proxies.values()))
        columns = ["scheme", "host", "port"] + list(sample_proxy_data.serialize_json().keys())

        with open(args.output_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(columns)

            for (scheme, host, port), proxy_data in proxies.items():
                row = [scheme, host, port] + list(proxy_data.serialize_json().values())
                writer.writerow(row)
    elif args.output_type == "txt":
        with open(args.output_file, "w") as f:
            for (scheme, host, port), proxy_data in proxies.items():
                auth = proxy_data.auth
                if auth is not None:
                    f.write(f"{scheme}:{host}:{port}:{auth.login}:{auth.password}\n")
                else:
                    f.write(f"{scheme}:{host}:{port}\n")

    print(f"...Done! Proxies exported to {args.output_file}")


if __name__ == "__main__":
    main()