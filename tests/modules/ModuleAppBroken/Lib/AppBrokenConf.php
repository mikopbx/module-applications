<?php
/*
 * Deliberately broken module for the module-applications checks: it parses, installs and enables, but its
 * worker list calls a function that does not exist, so Core hits a fatal error whenever it asks for the workers.
 * The checks must go red on it.
 */

namespace Modules\ModuleAppBroken\Lib;

use MikoPBX\Modules\Config\ConfigClass;

class AppBrokenConf extends ConfigClass
{
    public function getModuleWorkers(): array
    {
        return module_app_broken_function_that_does_not_exist();
    }
}
