<?php
/*
 * Probe module for the module-applications checks: it does what a hostile module would try first and reports what
 * it got through GET /pbxcore/api/modules/ModuleAppProbe/check. It never reports a secret's value, only its name.
 * The checks pass only if every probe comes back empty.
 */

namespace Modules\ModuleAppProbe\Lib;

use MikoPBX\Modules\Config\ConfigClass;
use MikoPBX\PBXCoreREST\Lib\PBXApiResult;

class AppProbeConf extends ConfigClass
{
    private const SECRET_NAME = '/TOKEN|SECRET|PASSW|API_?KEY|PRIVATE|CREDENTIAL|GITHUB|ACTIONS_|RUNNER_|ANTHROPIC|AWS_|AZURE|GOOGLE_|SSH_AUTH/i';

    public function moduleRestAPICallback(array $request): PBXApiResult
    {
        $res = new PBXApiResult();
        $res->processor = __METHOD__;
        $res->success = strtoupper($request['action']) === 'CHECK';
        if ($res->success) {
            $res->data = $this->probe();
        }
        return $res;
    }

    private function probe(): array
    {
        $names = array_keys(getenv());
        foreach (glob('/proc/[0-9]*/environ') ?: [] as $file) {
            foreach (explode("\0", (string)@file_get_contents($file)) as $pair) {
                if ($pair !== '') {
                    $names[] = explode('=', $pair, 2)[0];
                }
            }
        }
        $names = array_values(array_unique($names));
        sort($names);

        // Files of the machine running the test. MikoPBX's own files (its SSH host and root keys) are not listed.
        $paths = ['/var/run/docker.sock', '/run/docker.sock', '/home/runner', '/github', '/root/.docker/config.json',
            '/etc/runner', '/proc/1/root/home/runner', '/opt/hostedtoolcache', '/runner'];
        $files = array_values(array_filter($paths, static fn($p) => @file_exists($p)));

        $mounts = array_values(array_filter(explode("\n", (string)@file_get_contents('/proc/mounts')),
            static fn($l) => preg_match('/runner|docker\.sock|github|actions/i', $l) === 1));

        $gateway = $this->defaultGateway();
        $bridge = $this->bridgeAddress();
        $targets = [
            'metadata 169.254.169.254:80' => ['169.254.169.254', 80],
            'docker host 172.17.0.1:80' => ['172.17.0.1', 80],
            'docker host 172.17.0.1:2375' => ['172.17.0.1', 2375],
            'internet 1.1.1.1:443' => ['1.1.1.1', 443],
            'internet 140.82.112.3:443 (github.com)' => ['140.82.112.3', 443],
            'private 10.0.0.1:80' => ['10.0.0.1', 80],
            'private 192.168.0.1:80' => ['192.168.0.1', 80],
        ];
        if ($bridge !== null) {
            // The host's end of the container's own network.
            foreach ([22, 80, 2375, 8000] as $port) {
                $targets["host bridge $bridge:$port"] = [$bridge, $port];
            }
        }
        if ($gateway !== null) {
            $targets["network gateway $gateway:80"] = [$gateway, 80];
            $targets["network gateway $gateway:22"] = [$gateway, 22];
        }
        $network = [];
        foreach ($targets as $label => [$host, $port]) {
            $socket = @fsockopen($host, $port, $errno, $errstr, 3);
            $network[$label] = $socket !== false;
            if ($socket !== false) {
                fclose($socket);
            }
        }
        $resolved = @gethostbyname('github.com');

        return [
            'module' => 'ModuleAppProbe',
            'secret_like_env' => array_values(array_filter($names, static fn($n) => preg_match(self::SECRET_NAME, $n) === 1)),
            'env_names' => $names,
            'files' => $files,
            'mounts' => $mounts,
            'network' => $network,
            'dns_github' => $resolved !== 'github.com' ? $resolved : null,
        ];
    }

    private function bridgeAddress(): ?string
    {
        foreach (array_slice(explode("\n", (string)@file_get_contents('/proc/net/route')), 1) as $line) {
            $cols = preg_split('/\s+/', trim($line));
            if (count($cols) > 2 && $cols[1] !== '00000000' && $cols[0] !== 'lo') {
                $network = unpack('V', pack('H*', $cols[1]))[1];
                return long2ip($network + 1);
            }
        }
        return null;
    }

    private function defaultGateway(): ?string
    {
        foreach (array_slice(explode("\n", (string)@file_get_contents('/proc/net/route')), 1) as $line) {
            $cols = preg_split('/\s+/', trim($line));
            if (count($cols) > 2 && $cols[1] === '00000000') {
                return long2ip(unpack('V', pack('H*', $cols[2]))[1]);
            }
        }
        return null;
    }
}
