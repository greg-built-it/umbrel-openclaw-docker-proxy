import sys
import httpx

SOCKET_PATH = "/run/proxy/docker-proxy.sock"


def main() -> int:
    try:
        transport = httpx.HTTPTransport(uds=SOCKET_PATH)
        with httpx.Client(transport=transport, timeout=5.0) as client:
            r = client.get("http://localhost/health")
            if r.status_code != 200:
                print("healthcheck_failed: unexpected_status")
                return 1
            if r.text != "ok":
                print("healthcheck_failed: unexpected_body")
                return 1
            return 0
    except Exception:
        print("healthcheck_failed: unreachable")
        return 1


if __name__ == "__main__":
    sys.exit(main())
