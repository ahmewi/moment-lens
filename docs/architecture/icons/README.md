# Azure architecture icons

The SVG files in this folder are icons from the official **Azure Architecture Icons** set published by Microsoft
(download and terms: <https://learn.microsoft.com/azure/architecture/icons/>). They are used here, **unmodified**, only to
draw the [architecture diagram](../architecture.svg). The copies come from the same set as redistributed in Microsoft's
`Azure-Samples/Spec2Cloud` repository; refresh them from the official download whenever Microsoft publishes a new release.

| File | Azure service shown next to it in the diagram |
| --- | --- |
| `10230-icon-service-Users.svg` | Users |
| `10035-icon-service-App-Services.svg` | Azure App Service |
| `01800-icon-service-Azure-Video-Indexer.svg` | Azure AI Video Indexer |
| `02989-icon-service-Container-Apps-Environments.svg` | Azure Container Apps |
| `03438-icon-service-Azure-OpenAI.svg` | Azure OpenAI |
| `10044-icon-service-AI-Search.svg` | Azure AI Search |
| `10086-icon-service-Storage-Accounts.svg` | Azure Storage |
| `02579-icon-service-Private-Endpoints.svg` | Private endpoints (Azure Private Link) |
| `10227-icon-service-Managed-Identities.svg` | Managed identities |
| `10061-icon-service-Virtual-Networks.svg` | Azure Virtual Network |
| `10105-icon-service-Container-Registries.svg` | Azure Container Registry |
| `00012-icon-service-Application-Insights.svg` | Application Insights |
| `00009-icon-service-Log-Analytics-Workspaces.svg` | Log Analytics |
| `10007-icon-service-Resource-Groups.svg` | Resource group |

## Terms of use

Microsoft publishes the icons under these terms:

> Microsoft permits the use of these icons in architectural diagrams, training materials, or documentation. You may
> copy, distribute, and display the icons only for the permitted use unless granted explicit permission by Microsoft.
> Microsoft reserves all other rights.

Microsoft's guidance for the icons, which the diagram follows:

- Use the icons to illustrate how products work together, and show the product name close to each icon.
- Use the icons as they appear within Azure. Do not crop, flip, rotate or distort them.
- Do not use Microsoft product icons to represent your own product or service.

The icons are Microsoft's, not part of this project's own code: do not reuse them outside architecture diagrams, training
material or documentation.

## Updating the diagram

`../build_diagram.py` embeds the icons into `../architecture.svg` (as data URIs, so the SVG renders on GitHub, which does
not load files referenced from an SVG shown as an image). To add or refresh an icon, copy the SVG you need into this
folder without editing it, reference it in `build_diagram.py` and run:

```bash
python3 docs/architecture/build_diagram.py
```

The script needs only the Python standard library.
