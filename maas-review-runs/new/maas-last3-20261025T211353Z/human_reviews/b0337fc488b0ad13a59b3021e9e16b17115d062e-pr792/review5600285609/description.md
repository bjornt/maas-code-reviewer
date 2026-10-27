Adds the security documentation as part of SSDLC compliance.

- Security page (`explanation/security.md`)
  - Reporting vulnerabilities: the Ubuntu disclosure policy, a link to SECURITY.md, and the supported MAAS versions.
  - Security architecture: a Mermaid diagram and a short description of the components, the trust boundaries and the security controls at each boundary.
  - Risks and recommended controls: the risks inherent to MAAS and the controls users are responsible for, in the context of the MAAS threat model.

- Decommission MAAS (`how-to-guides/decommission-maas.md`): a new guide to remove MAAS securely. It covers exporting data, releasing machines with disk erase, revoking access, removing the controllers, deleting data in external services, and sanitizing disks.

- Report a bug (`uncategorized/report-a-bug.md`): added a warning not to report security vulnerabilities in Launchpad, linking to the new section.

- Mermaid support: enables `sphinxcontrib-mermaid` in `conf.py` and `requirements.txt`, to render the architecture diagram.
