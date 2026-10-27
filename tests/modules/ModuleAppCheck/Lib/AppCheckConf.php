<?php
/*
 * Control module for module-applications, derived from mikopbx/ModuleTemplate (GPL-3.0-or-later).
 */

namespace Modules\ModuleAppCheck\Lib;

use MikoPBX\Core\Workers\Cron\WorkerSafeScriptsCore;
use MikoPBX\Modules\Config\ConfigClass;
use MikoPBX\PBXCoreREST\Lib\PBXApiResult;

class AppCheckConf extends ConfigClass
{
    /**
     * One worker, so the checks can see a module service start on enable and stop on disable.
     */
    public function getModuleWorkers(): array
    {
        return [
            [
                'type'   => WorkerSafeScriptsCore::CHECK_BY_BEANSTALK,
                'worker' => WorkerAppCheckMain::class,
            ],
        ];
    }

    /**
     * GET /pbxcore/api/modules/ModuleAppCheck/check answers with the module's version, so the checks can tell the
     * module's own API works.
     */
    public function moduleRestAPICallback(array $request): PBXApiResult
    {
        $res = new PBXApiResult();
        $res->processor = __METHOD__;
        if (strtoupper($request['action']) === 'CHECK') {
            $res->success = true;
            $res->data = ['module' => 'ModuleAppCheck', 'status' => 'working'];
        } else {
            $res->success = false;
            $res->messages[] = 'API action not found in moduleRestAPICallback ModuleAppCheck';
        }
        return $res;
    }
}
