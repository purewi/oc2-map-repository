import os


os.environ["OC2_CLIENT_VARIANT"] = "cos"

from client.main import main  # noqa: E402


raise SystemExit(main())
