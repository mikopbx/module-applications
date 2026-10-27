<?php
/*
 * Control module for module-applications, derived from mikopbx/ModuleTemplate (GPL-3.0-or-later).
 */

namespace Modules\ModuleAppPaid\Lib;

use MikoPBX\Common\Handlers\CriticalErrorsHandler;
use MikoPBX\Core\System\BeanstalkClient;
use MikoPBX\Core\Workers\WorkerBase;

require_once 'Globals.php';

class WorkerAppPaidMain extends WorkerBase
{
    public function start(array $argv): void
    {
        $client = new BeanstalkClient(self::class);
        $client->subscribe($this->makePingTubeName(self::class), [$this, 'pingCallBack']);
        while (true) {
            $client->wait();
        }
    }
}

// Start worker process
$workerClassname = WorkerAppPaidMain::class;
if (isset($argv) && count($argv) > 1) {
    cli_set_process_title($workerClassname);
    try {
        $worker = new $workerClassname();
        $worker->start($argv);
    } catch (\Throwable $e) {
        CriticalErrorsHandler::handleExceptionWithSyslog($e);
    }
}
