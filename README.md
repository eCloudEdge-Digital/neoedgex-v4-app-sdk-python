# NeoEdgeX App SDK Python v4

NeoEdgeX App SDK Python v4 is the public Python SDK for building NeoEdgeX node applications such as drivers, protocol adapters, forwarders, and processors.

- Package name: `neoedgex-v4-app-sdk-python`
- Third-party app packages:
  - `neoedgex`
  - `neoedgex.mock`
  - `neoedgex.testutil` (unit tests only)

For normal app development, use the `neoedgex` package as the public SDK surface. Types such as `Node`, `Message`, and `NodeEnv` are available there directly.

**Version compatibility:** NeoFlow data messages are CBOR, encoded to a fixed message format — the developer guide's Message Model section states it in full. SDK 1.x apps cannot exchange NeoFlow messages with 2.0.0 apps; see the changelog in the developer guides for the breaking changes and migration steps.

Start with the external developer guides:

- [Developer Guide (English)](./docs/developer-guide.en.md)
- [第三方開發指南（繁體中文）](./docs/developer-guide.zh-tw.md)

Install:

```bash
python3 -m pip install "neoedgex-v4-app-sdk-python @ git+https://github.com/eCloudEdge-Digital/neoedgex-v4-app-sdk-python.git"
```

The runtime dependencies (`paho-mqtt` for transport, `cbor2` for the message encoding) are declared by the package and installed automatically.

If you need to install a specific version, use the release tag instead:

```bash
python3 -m pip install "neoedgex-v4-app-sdk-python @ git+https://github.com/eCloudEdge-Digital/neoedgex-v4-app-sdk-python.git@v2.1.0"
```

A minimal app implements `neoedgex.NodeHandler` and starts with `neoedgex.new(...).run()`:

```python
import neoedgex


class ExampleApp:
    def handle(self, ctx: neoedgex.NodeEnv) -> None:
        for _msg in ctx.messages():
            try:
                ctx.publish("output1", {"power": 42.0})
            except Exception as err:
                ctx.report_error(neoedgex.CodeProcessError, err)


if __name__ == "__main__":
    neoedgex.new(ExampleApp()).run()
```

The `output1` handle and the `power` key must be declared in the node's output schema — see the developer guide's Output Schema section.

For internal architecture and implementation notes, see [DESIGN.md](./DESIGN.md).
