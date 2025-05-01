import argparse
from pathlib import Path

from . import *


def main():
    argparser = argparse.ArgumentParser()
    argparser.add_argument("proxies_txt_file", type=str)
    argparser.add_argument("proxies_json_file", type=str, default="proxies.json", nargs="?")
    args = argparser.parse_args()

    proxy_provider = ProxyProvider(Path(args.proxies_json_file))
    total = 0
    new = 0
    with open(args.proxies_txt_file, "r") as f:
        for line in f.readlines():
            scheme, host, port, *_creds = line.rsplit(":", 4)

            if len(_creds) == 2:
                creds = BasicAuth(_creds[0], _creds[1])
            elif len(_creds) == 1:
                print(f"-> Could not parse proxy {line!r}.")
                continue
            else:
                creds = None

            total += 1

            port = int(port)
            if (scheme, host, port) in proxy_provider.proxies.proxies:
                print(f"-> Proxy {host!r}:{port!r} already exists.")
                continue
            proxy_provider.proxies.add_proxy(Proxy(scheme, host, int(port), creds))
            new += 1

    proxy_provider.store_proxies()

    print(f"...Done! New proxies: {new}/{total} = {new/total:%}")


if __name__ == "__main__":
    main()
