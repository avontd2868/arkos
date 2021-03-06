import json
import ldap
import ldap.modlist
import os
import sys
import xmlrpclib

from dbus import SystemBus, Interface


class ConnectionsManager:
    def __init__(self, config):
        self.LDAP = ldap_connect(config=config)
        self.SystemD = systemd_connect()
        self.Supervisor = supervisor_connect()


def ldap_connect(uri="", rootdn="", dn="cn=admin", config=None, passwd=""):
    if not all([uri, rootdn, dn]) and not config:
        raise Exception("No configuration values passed")
    uri = uri or config.get("general", "ldap_uri", "ldap://localhost")
    rootdn = rootdn or config.get("general", "ldap_rootdn", "dc=arkos-servers,dc=org")
    if not passwd:
        if os.path.isfile(os.path.join(sys.path[0], 'secrets.json')):
            secrets = os.path.join(sys.path[0], 'secrets.json')
        else:
            secrets = "/etc/arkos/secrets.json"
        with open(secrets, "r") as f:
            passwd = json.loads(f.read())
            if passwd.has_key("ldap"):
                passwd = passwd["ldap"]
            else:
                raise Exception("Admin LDAP credentials not found in secrets file")
    c = ldap.initialize(uri)
    try:
        c.simple_bind_s("%s,%s" % (dn, rootdn), passwd)
    except ldap.INVALID_CREDENTIALS:
        raise Exception("Admin LDAP authentication failed.")
    if dn != "cn=admin":
        data = c.search_s("cn=admins,ou=groups,%s" % rootdn,
            ldap.SCOPE_SUBTREE, "(objectClass=*)", ["member"])[0][1]["member"]
        if "%s,%s" % (dn, rootdn) not in data:
            raise Exception("User is not an administrator")        
    return c

def systemd_connect():
    bus = SystemBus()
    systemd = bus.get_object("org.freedesktop.systemd1", "/org/freedesktop/systemd1")
    return Interface(systemd, dbus_interface="org.freedesktop.systemd1.Manager")

def supervisor_connect():
    s = xmlrpclib.Server("http://localhost:9001/RPC2")
    return s.supervisor
