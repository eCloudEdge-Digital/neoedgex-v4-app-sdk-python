# NeoEdgeX App SDK Python v4

NeoEdgeX App SDK Python v4 is the public Python SDK for building NeoEdgeX node applications.

- Package name: `neoedgex-v4-app-sdk-python`
- Third-party app packages:
  - `neoedgex`
  - `neoedgex.mock`

For normal app development, use the `neoedgex` package as the public SDK surface. Types such as `Node`, `Message`, and `NodeEnv` are available there directly.

Start with the external developer guides:

- [Developer Guide (English)](./docs/developer-guide.en.md)
- [第三方開發指南（繁體中文）](./docs/developer-guide.zh-tw.md)

Install:

```bash
python3 -m pip install "neoedgex-v4-app-sdk-python @ git+https://github.com/eCloudEdge-Digital/neoedgex-v4-app-sdk-python.git"
```

If you need to install a specific version, use the release tag instead:

```bash
python3 -m pip install "neoedgex-v4-app-sdk-python @ git+https://github.com/eCloudEdge-Digital/neoedgex-v4-app-sdk-python.git@v1.0.0"
```

For internal architecture and implementation notes, see [DESIGN.md](./DESIGN.md).
