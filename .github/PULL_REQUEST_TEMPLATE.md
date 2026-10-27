## Application for `applications/<module_id>`

I have checked each of the following before submitting (tick each box by changing `[ ]` to `[x]`):

- [ ] I maintain this module and am, or act for, its publisher in the MIKO cabinet (`publisher_id`).
- [ ] I accept the [module-applications terms](https://github.com/mikopbx/module-applications/blob/main/TERMS.md).
- [ ] This pull request changes only `applications/<module_id>/application.yaml`, and `tools/validate.py --online <module_id>` passes locally.
- [ ] `tools/check <module_id>` passes locally: the module installs, enables, disables and uninstalls, and MikoPBX still works.
- [ ] `support_email` is a public address, the same in `module.json`, here and on my publisher card.
- [ ] `commit` is the full commit SHA I want reviewed, and it is in the public repository `repo`.

To sign the [MikoPBX Publisher Agreement](https://github.com/mikopbx/module-applications/blob/main/PUBLISHER_AGREEMENT.md), post a comment on this pull request that contains only this line: `I have read the MikoPBX Publisher Agreement and I hereby sign it`

To update an application, change `commit` (or another field) in it; to withdraw a module, remove `applications/<module_id>/`. See CONTRIBUTING.md.

What the module does and anything a reviewer should know (optional):
