<?php
/*
 * Minimal MikoPBX module for an application to module-applications.
 * Based on mikopbx/ModuleTemplate (GPL-3.0-or-later). Rename ModuleExample everywhere to your module_id.
 *
 * Rule CODE-001: this file declares namespace Modules\<module_id>\Setup and class PbxExtensionSetup.
 * PbxExtensionSetupBase installs the module (checkCompatibility, files, database, registration) and uninstalls it.
 */

namespace Modules\ModuleExample\Setup;

use MikoPBX\Modules\Setup\PbxExtensionSetupBase;

class PbxExtensionSetup extends PbxExtensionSetupBase
{
}
