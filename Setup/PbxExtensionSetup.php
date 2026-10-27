<?php
/*
 * Test module with a broken contract: module.json is out of range and the setup class is in the wrong namespace.
 * tools/validate.py --online must reject it without running anything.
 */

namespace Modules\ModuleAppCheck\Setup;

use MikoPBX\Modules\Setup\PbxExtensionSetupBase;

class PbxExtensionSetup extends PbxExtensionSetupBase
{
}
