# ModuleAppProbe

Probe module for the module-applications checks. Its REST callback `check` reports which secret-like environment
variables, host files, mounts and network destinations a module can reach from inside the test container (names
and true/false only, never values). The negative test expects every list empty and every connection false.
Derived from mikopbx/ModuleTemplate, GPL-3.0-or-later.
