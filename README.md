# ModuleAppPaid

Paid control module for the module-applications checks (rule LIFE-007): the same code as ModuleAppCheck, with
`lic_product_id` and `lic_feature_id` set (made-up ids, no licence exists for them). Without a licence key MikoPBX refuses
to install it; the test copy without licence ids installs, enables one worker (`WorkerAppPaidMain`), answers
`GET /pbxcore/api/modules/ModuleAppPaid/check`, disables and uninstalls cleanly. Derived from
[mikopbx/ModuleTemplate](https://github.com/mikopbx/ModuleTemplate), GPL-3.0-or-later.
