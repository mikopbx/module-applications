<?php
/*
 * Minimal MikoPBX module for an application to module-applications.
 * Based on mikopbx/ModuleTemplate (GPL-3.0-or-later).
 *
 * Rule CODE-003: the Conf class is Lib/<X>Conf.php, where X is module_id without "Module"
 * (ModuleExample -> ExampleConf), and extends ConfigClass. Core loads it at every boot; a fatal error disables the module.
 */

namespace Modules\ModuleExample\Lib;

use MikoPBX\Modules\Config\ConfigClass;
use MikoPBX\PBXCoreREST\Lib\PBXApiResult;

class ExampleConf extends ConfigClass
{
    /**
     * Workers the module needs, as ['type' => WorkerSafeScriptsCore::CHECK_BY_*, 'worker' => Class::class].
     * tools/check looks for them after enable and makes sure they stop on disable.
     */
    public function getModuleWorkers(): array
    {
        return [];
    }

    /**
     * GET /pbxcore/api/modules/ModuleExample/check. tools/check calls it after enable; answer result=true when the
     * module works.
     */
    public function moduleRestAPICallback(array $request): PBXApiResult
    {
        $res = new PBXApiResult();
        $res->processor = __METHOD__;
        $res->success = strtoupper($request['action']) === 'CHECK';
        if (!$res->success) {
            $res->messages[] = 'API action not found in moduleRestAPICallback ModuleExample';
        }
        return $res;
    }
}
