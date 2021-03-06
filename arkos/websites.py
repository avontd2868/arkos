import ConfigParser
import git
import os
import nginx
import re
import shutil

from arkos import config, storage, applications, certificates
from arkos import databases, tracked_services
from arkos.system import users, groups, filesystems
from arkos.utilities import shell, random_string, DefaultMessage


# If no cipher preferences set, use the default ones
# As per Mozilla recommendations, but substituting 3DES for RC4
ciphers = ':'.join([
    'ECDHE-RSA-AES128-GCM-SHA256', 'ECDHE-ECDSA-AES128-GCM-SHA256',
    'ECDHE-RSA-AES256-GCM-SHA384', 'ECDHE-ECDSA-AES256-GCM-SHA384',
    'kEDH+AESGCM', 'ECDHE-RSA-AES128-SHA256', 
    'ECDHE-ECDSA-AES128-SHA256', 'ECDHE-RSA-AES128-SHA', 
    'ECDHE-ECDSA-AES128-SHA', 'ECDHE-RSA-AES256-SHA384',
    'ECDHE-ECDSA-AES256-SHA384', 'ECDHE-RSA-AES256-SHA', 
    'ECDHE-ECDSA-AES256-SHA', 'DHE-RSA-AES128-SHA256',
    'DHE-RSA-AES128-SHA', 'DHE-RSA-AES256-SHA256', 
    'DHE-DSS-AES256-SHA', 'AES128-GCM-SHA256', 'AES256-GCM-SHA384',
    'ECDHE-RSA-DES-CBC3-SHA', 'ECDHE-ECDSA-DES-CBC3-SHA',
    'EDH-RSA-DES-CBC3-SHA', 'EDH-DSS-DES-CBC3-SHA', 
    'DES-CBC3-SHA', 'HIGH', '!aNULL', '!eNULL', '!EXPORT', '!DES',
    '!RC4', '!MD5', '!PSK'
    ])


class Site:
    def __init__(
            self, id=0, name="", addr="", port=80, path="", php=False, version="", 
            cert=None, db=None, data_path="", block=[], enabled=False):
        self.id = id or get_new_id()
        self.name = name
        self.path = path
        self.addr = addr
        self.port = port
        self.php = php
        self.version = version
        self.cert = None
        self.db = None
        self.meta = None
        self.enabled = enabled
        self.data_path = data_path
        self.addtoblock = block
        self.installed = False
    
    def install(self, meta, extra_vars={}, enable=True, message=DefaultMessage()):
        from arkos import backup
        self.meta = meta
        if message:
            message.update("info", "Preparing site install...")
        specialmsg = ''
        site_dir = config.get("websites", "site_dir")
        self.path = self.path or os.path.join(site_dir, self.name)

        if not self.meta.download_url:
            ending = ''
        elif self.meta.download_url.endswith('.tar.gz'):
            ending = '.tar.gz'
        elif self.meta.download_url.endswith('.tgz'):
            ending = '.tgz'
        elif self.meta.download_url.endswith('.tar.bz2'):
            ending = '.tar.bz2'
        elif self.meta.download_url.endswith('.zip'):
            ending = '.zip'
        elif self.meta.download_url.endswith('.git'):
            ending = '.git'
        else:
            raise InstallError('Only GIT repos, gzip, bzip, and zip packages supported for now')

        if message:
            message.update("info", "Running pre-installation...")
        # Run webapp preconfig, if any
        try:
            self.pre_install(extra_vars)
        except Exception, e:
            raise Exception('Error during website config - '+str(e))

        if self.meta.selected_dbengine:
            if message:
                message.update("info", "Creating database...")
            try:
                self.db = databases.new(self.meta.selected_dbengine, self.name)
            except Exception, e:
                raise InstallError('Databases could not be created - %s' % str(e))

        # Make sure the target directory exists, but is empty
        # Testing for sites with the same name should have happened by now
        pkg_path = '/tmp/'+self.name+ending
        if os.path.isdir(self.path):
            shutil.rmtree(self.path)
        os.makedirs(self.path)

        if message:
            message.update("info", "Downloading website source...")
        # Download and extract the source package
        if self.meta.download_url and ending == '.git':
            git.Repo.clone_from(self.meta.download_url, self.path)
        elif self.meta.download_url:
            try:
                download(self.meta.download_url, file=pkg_path, crit=True)
            except Exception, e:
                raise InstallError('Couldn\'t download - %s' % str(e))

            if ending in ['.tar.gz', '.tgz', '.tar.bz2']:
                extract_cmd = 'tar '
                extract_cmd += 'xzf' if ending in ['.tar.gz', '.tgz'] else 'xjf'
                extract_cmd += ' /tmp/%s -C %s --strip 1' % (self.name+ending, self.path)
            else:
                extract_cmd = 'unzip -d %s /tmp/%s' % (self.path, self.name+ending)

            if message:
                message.update("info", "Installing site...")
            status = shell(extract_cmd)
            if status["code"] >= 1:
                raise InstallError(status["stderr"])
            os.remove(pkg_path)

        self.php = extra_vars.get("php") or self.php
        self.addtoblock = extra_vars.get("addtoblock") or self.addtoblock

        if message:
            message.update("info", "Configuring webserver...")
        if addtoblock:
            addtoblock = nginx.loads(addtoblock, False)
        else:
            addtoblock = []
        if isinstance(self, Website) and self.php and addtoblock:
            addtoblock.extend(x for x in phpblock)
        elif isinstance(self, Website) and self.php:
            addtoblock = phpblock
        self.php = self.php or self.meta.uses_php or False
        self.version = self.meta.version.rsplit("-", 1)[0] if self.meta.website_updates else None

        # If there is a custom path for the data directory, do the magic
        if self.meta.website_datapaths and self.data_path:
            uid, gid = users.get_system("http").uid, groups.get_system("http").gid
            if not os.path.exists(datadir):
                os.makedirs(datadir)
            for r, d, f in os.walk(datadir):
                for x in d:
                    os.chmod(os.path.join(r, x), 0755)
                    os.chown(os.path.join(r, x), uid, gid)
                for x in f:
                    os.chmod(os.path.join(r, x), 0644)
                    os.chown(os.path.join(r, x), uid, gid)
            extra_vars["datadir"] = self.data_path
        elif hasattr(self, "website_default_data_subdir"):
            self.data_path = os.path.join(self.path, self.website_default_data_subdir)
        else:
            self.data_path = self.path

        # Setup the webapp and create an nginx serverblock
        try:
            c = nginx.Conf()
            s = nginx.Server(
                nginx.Key('listen', self.port),
                nginx.Key('server_name', self.addr),
                nginx.Key('root', self.path),
                nginx.Key('index', 'index.'+('php' if self.php else 'html'))
            )
            if add:
                s.add(*[x for x in add])
            c.add(s)
            nginx.dumpf(c, os.path.join('/etc/nginx/sites-available', self.name))
            c = ConfigParser.SafeConfigParser()
            c.add_section('website')
            c.set('website', 'name', self.name)
            c.set('website', 'type', self.meta.id)
            c.set('website', 'ssl', '')
            c.set('website', 'version', self.version)
            if self.meta.website_datapaths and self.data_path:
                c.set('website', 'data_path', self.data_path)
            c.set('website', 'dbengine', self.selected_dbengine or '')
            with open(os.path.join(self.path, ".arkos"), 'w') as f:
                c.write(f)
        except Exception, e:
            raise Exception('nginx serverblock couldn\'t be written - '+str(e))

        if message:
            message.update("info", "Running post-installation...")
        try:
            specialmsg = self.post_install(extra_vars)
        except Exception, e:
            shutil.rmtree(self.path, True)
            self.nginx_remove(w, False)
            raise Exception('Error during website config - '+str(e))

        if message:
            message.update("info", "Finishing...")
        if enable:
            try:
                self.nginx_enable()
            except:
                raise ReloadError('nginx')
        if enable and self.php:
            try:
                php_reload()
            except:
                raise ReloadError('PHP-FPM')
        filesystems.register_point(self.name, self.data_path, "site", self.meta.icon)
        tracked_services.register(self.meta.id if self.meta else "website", 
            self.name, self.name, "gen-earth", [("tcp", self.port)], 2)
        self.backup = self.meta.get_module("backup") or backup.BackupController
        self.backup = self.backup(self.name, self)
        self.installed = True
        storage.sites.add("sites", self)
        if specialmsg:
            return specialmsg
    
    def ssl_enable(self):
        if config.get("certificates", "ciphers"):
            ciphers = config.get("certificates", "ciphers")
        else:
            config.set("certificates", "ciphers", ciphers)
            config.save()

        c = nginx.loadf(os.path.join('/etc/nginx/sites-available/', self.name))
        s = c.servers[0]
        l = s.filter('Key', 'listen')[0]
        if l.value == '80':
            l.value = '443 ssl'
            c.add(nginx.Server(
                nginx.Key('listen', '80'),
                nginx.Key('server_name', self.addr),
                nginx.Key('return', '301 https://%s$request_uri' % self.addr)
            ))
            for x in c.servers:
                if x.filter('Key', 'listen')[0].value == '443 ssl':
                    s = x
                    break
        else:
            l.value = l.value.split(' ssl')[0] + ' ssl'
        for x in s.all():
            if type(x) == nginx.Key and x.name.startswith('ssl_'):
                s.remove(x)
        s.add(
            nginx.Key('ssl_certificate', self.cert.cert_path),
            nginx.Key('ssl_certificate_key', self.cert.key_path),
            nginx.Key('ssl_protocols', 'TLSv1 TLSv1.1 TLSv1.2'),
            nginx.Key('ssl_ciphers', ciphers),
            nginx.Key('ssl_session_timeout', '5m'),
            nginx.Key('ssl_prefer_server_ciphers', 'on'),
            nginx.Key('ssl_session_cache', 'shared:SSL:50m'),
            )
        nginx.dumpf(c, os.path.join('/etc/nginx/sites-available/', self.name))
        self.enable_ssl()
    
    def ssl_disable(self):
        c = nginx.loadf(os.path.join('/etc/nginx/sites-available/', self.name))
        if len(c.servers) > 1:
            for x in c.servers:
                if not 'ssl' in x.filter('Key', 'listen')[0].value \
                and x.filter('key', 'return'):
                    c.remove(x)
                    break
        s = c.servers[0]
        l = s.filter('Key', 'listen')[0]
        if l.value == '443 ssl':
            l.value = '80'
        else:
            l.value = l.value.rstrip(' ssl')
        s.remove(*[x for x in s.filter('Key') if x.name.startswith('ssl_')])
        nginx.dumpf(c, os.path.join('/etc/nginx/sites-available/', self.name))
        self.disable_ssl()
    
    def nginx_enable(self, reload=True):
        origin = os.path.join('/etc/nginx/sites-available', self.name)
        target = os.path.join('/etc/nginx/sites-enabled', self.name)
        if not os.path.exists(target):
            os.symlink(origin, target)
            self.enabled = True
        if reload == True:
            nginx_reload()
    
    def nginx_disable(self, reload=True):
        os.unlink(os.path.join('/etc/nginx/sites-enabled', self.name))
        self.enabled = False
        if reload == True:
            nginx_reload()
    
    def edit(self, oldname=""):
        # Update the nginx serverblock
        from arkos import backup
        c = nginx.loadf(os.path.join('/etc/nginx/sites-available', oldname or self.name))
        s = c.servers[0]
        if self.cert and self.port == 443:
            for x in c.servers:
                if x.filter('Key', 'listen')[0].value == '443 ssl':
                    s = x
            if self.port != 443:
                for x in c.servers:
                    if not 'ssl' in x.filter('Key', 'listen')[0].value \
                    and x.filter('key', 'return'):
                        c.remove(x)
        elif self.port == 443:
            c.add(nginx.Server(
                nginx.Key('listen', '80'),
                nginx.Key('server_name', self.addr),
                nginx.Key('return', '301 https://%s$request_uri'%self.addr)
            ))
        # If the name was changed, rename the folder and files
        if oldname and self.name != oldname:
            if self.path.endswith('_site'):
                self.path = os.path.join(config.get("websites", "site_dir"), self.name, '_site')
            elif self.path.endswith('htdocs'):
                self.path = os.path.join(config.get("websites", "site_dir"), self.name, 'htdocs')
            else:
                self.path = os.path.join(config.get("websites", "site_dir"), self.name)
            if os.path.exists(self.path):
                shutil.rmtree(self.path)
            shutil.move(os.path.join(config.get("websites", "site_dir"), oldname), self.path)
            shutil.move(os.path.join('/etc/nginx/sites-available', oldname),
                os.path.join('/etc/nginx/sites-available', self.name))
            os.unlink(os.path.join("/etc/nginx/sites-available", oldname))
            self.nginx_enable(reload=False)
        s.filter('Key', 'listen')[0].value = str(self.port)+' ssl' if self.cert else str(self.port)
        s.filter('Key', 'server_name')[0].value = self.addr
        s.filter('Key', 'root')[0].value = self.path
        s.filter('Key', 'index')[0].value = 'index.php' if self.php else 'index.html'
        nginx.dumpf(c, os.path.join('/etc/nginx/sites-available', oldname))
        tracked_services.register(self.meta.id if self.meta else "website", 
            self.name, self.name, self.icon, [("tcp", self.port)], 2)
        self.backup = self.meta.get_module("backup") or backup.BackupController
        self.backup = self.backup(self.name, self)
        nginx_reload()

    def update(self, message=DefaultMessage()):
        if self.version == self.meta.version.rsplit("-", 1)[0]:
            raise Exception("Website is already at the latest version")
        elif self.version in [None, "None"]:
            raise Exception("Updates not supported for this website type")
        if not self.meta.download_url:
            ending = ''
        elif self.meta.download_url.endswith('.tar.gz'):
            ending = '.tar.gz'
        elif self.meta.download_url.endswith('.tgz'):
            ending = '.tgz'
        elif self.meta.download_url.endswith('.tar.bz2'):
            ending = '.tar.bz2'
        elif self.meta.download_url.endswith('.zip'):
            ending = '.zip'
        elif self.meta.download_url.endswith('.git'):
            ending = '.git'
        else:
            raise InstallError('Only GIT repos, gzip, bzip, and zip packages supported for now')

        if message:
            message.update("info", "Downloading website source...")
        if self.download_url and ending == '.git':
            pkg_path = self.download_url 
        elif self.download_url:
            pkg_path = os.path.join('/tmp', self.name+ending)
            try:
                download(self.meta.download_url, file=pkg_path, crit=True)
            except Exception, e:
                raise Exception('Couldn\'t update - %s' % str(e))
        try:
            if message:
                message.update("info", "Updating website...")
            self.update_site(self.path, pkg_path, self.version)
        except Exception, e:
            raise Exception('Couldn\'t update - %s' % str(e))
        finally:
            self.version = self.meta.version.rsplit('-', 1)[0]
        if pkg_path:
            os.unlink(pkg_path)

    def remove(self, message=DefaultMessage()):
        if message:
            message.update("info", "Running pre-removal...")
        self.pre_remove()
        if message:
            message.update("info", "Removing website...")
        if self.path.endswith('_site'):
            shutil.rmtree(self.path.split('/_site')[0])
        elif self.path.endswith('htdocs'):
            shutil.rmtree(self.path.split('/htdocs')[0])
        elif os.path.islink(self.path):
            os.unlink(self.path)
        else:
            shutil.rmtree(self.path)
        if self.db:
            if message:
                message.update("info", "Removing database...")
            self.db.remove()
            self.db.usermod('del', '')
        self.nginx_disable(reload=True)
        tracked_services.deregister(self.meta.id if self.meta else "website", self.name)
        try:
            os.unlink(os.path.join('/etc/nginx/sites-available', self.name))
        except:
            pass
    
    def as_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "addr": self.addr,
            "port": self.port,
            "site_type": self.meta,
            "version": self.version,
            "certificate": self.cert.as_dict() if self.cert else None,
            "database": self.db.as_dict() if self.db else None,
            "php": self.php,
            "enabled": self.enabled
        }


class ReverseProxy:
    def __init__(
            self, id=0, name="", pretty_name="", path="", addr="", port=80, 
            base_path="", block=[], type="internal"):
        self.id = id or get_new_id()
        self.name = name
        self.pretty_name = pretty_name
        self.addr = addr
        self.path = path
        self.port = port
        self.base_path = base_path
        self.block = block
        self.type = type
        self.backup = None
        self.installed = False

    def install(self, extra_vars={}, enable=True, message=None):
        site_dir = config.get("websites", "site_dir")
        self.path = self.path or os.path.join(site_dir, self.name)
        if extra_vars:
			if not extra_vars.get('rp-type') or not vars.get('rp-pass'):
				raise Exception('Must enter ReverseProxy type and location to pass to')
			elif extra_vars.get('rp-type') in ['fastcgi', 'uwsgi']:
				self.block = [nginx.Location(vars.get('rp-lregex', '/'), 
					nginx.Key('%s_pass'%vars.get('rp-type'), 
						'%s'%vars.get('rp-pass')),
					nginx.Key('include', '%s_params'%vars.get('rp-type'))
					)]
			else:
				self.block = [nginx.Location(extra_vars.get('rp-lregex', '/'), 
					nginx.Key('proxy_pass', '%s'%extra_vars.get('rp-pass')),
					nginx.Key('proxy_redirect', 'off'),
					nginx.Key('proxy_buffering', 'off'),
					nginx.Key('proxy_set_header', 'Host $host')
					)]
			if vars.getvalue('rp-xrip', '') == '1':
				self.block[0].add(nginx.Key('proxy_set_header', 'X-Real-IP $remote_addr'))
			if vars.getvalue('rp-xff', '') == '1':
				self.block[0].add(nginx.Key('proxy_set_header', 'X-Forwarded-For $proxy_add_x_forwarded_for'))
        c = nginx.Conf()
        s = nginx.Server(
            nginx.Key('listen', self.port),
            nginx.Key('server_name', self.addr),
            nginx.Key('root', self.base_path or self.path),
            nginx.Key('index', 'index.'+('php' if self.php else 'html'))
        )
        if add:
            s.add(*[x for x in add])
        c.add(s)
        nginx.dumpf(c, os.path.join('/etc/nginx/sites-available', self.name))
        c = ConfigParser.SafeConfigParser()
        c.add_section('website')
        c.set('website', 'name', self.name)
        c.set('website', 'pretty_name', self.pretty_name)
        c.set('website', 'type', "ReverseProxy")
        c.set('website', 'extra', self.type)
        c.set('website', 'ssl', '')
        with open(os.path.join(self.path, ".arkos"), 'w') as f:
            c.write(f)
        tracked_services.register("website", self.name, self.pretty_name, 
            "gen-earth", [("tcp", self.port)], 2)
        self.installed = True
        storage.sites.add("sites", self)

    def remove(self, message=None):
        shutil.rmtree(self.path)
        self.nginx_disable(reload=True)
        tracked_services.deregister("website", self.name)
        try:
            os.unlink(os.path.join('/etc/nginx/sites-available', self.name))
        except:
            pass
    
    def ssl_enable(self):
        if config.get("certificates", "ciphers"):
            ciphers = config.get("certificates", "ciphers")
        else:
            config.set("certificates", "ciphers", ciphers)
            config.save()

        c = nginx.loadf(os.path.join('/etc/nginx/sites-available/', self.name))
        s = c.servers[0]
        l = s.filter('Key', 'listen')[0]
        if l.value == '80':
            l.value = '443 ssl'
            c.add(nginx.Server(
                nginx.Key('listen', '80'),
                nginx.Key('server_name', self.addr),
                nginx.Key('return', '301 https://%s$request_uri' % self.addr)
            ))
            for x in c.servers:
                if x.filter('Key', 'listen')[0].value == '443 ssl':
                    s = x
                    break
        else:
            l.value = l.value.split(' ssl')[0] + ' ssl'
        for x in s.all():
            if type(x) == nginx.Key and x.name.startswith('ssl_'):
                s.remove(x)
        s.add(
            nginx.Key('ssl_certificate', self.cert.cert_path),
            nginx.Key('ssl_certificate_key', self.cert.key_path),
            nginx.Key('ssl_protocols', 'TLSv1 TLSv1.1 TLSv1.2'),
            nginx.Key('ssl_ciphers', ciphers),
            nginx.Key('ssl_session_timeout', '5m'),
            nginx.Key('ssl_prefer_server_ciphers', 'on'),
            nginx.Key('ssl_session_cache', 'shared:SSL:50m'),
            )
        nginx.dumpf(c, os.path.join('/etc/nginx/sites-available/', self.name))
    
    def ssl_disable(self):
        c = nginx.loadf(os.path.join('/etc/nginx/sites-available/', self.name))
        if len(c.servers) > 1:
            for x in c.servers:
                if not 'ssl' in x.filter('Key', 'listen')[0].value \
                and x.filter('key', 'return'):
                    c.remove(x)
                    break
        s = c.servers[0]
        l = s.filter('Key', 'listen')[0]
        if l.value == '443 ssl':
            l.value = '80'
        else:
            l.value = l.value.rstrip(' ssl')
        s.remove(*[x for x in s.filter('Key') if x.name.startswith('ssl_')])
        nginx.dumpf(c, os.path.join('/etc/nginx/sites-available/', self.name))
    
    def nginx_enable(self, reload=True):
        origin = os.path.join('/etc/nginx/sites-available', self.name)
        target = os.path.join('/etc/nginx/sites-enabled', self.name)
        if not os.path.exists(target):
            os.symlink(origin, target)
            self.enabled = True
        if reload == True:
            nginx_reload()
    
    def nginx_disable(self, reload=True):
        os.unlink(os.path.join('/etc/nginx/sites-enabled', self.name))
        self.enabled = False
        if reload == True:
            nginx_reload()
    
    def as_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "pretty_name": self.pretty_name,
            "path": self.path,
            "type": self.type,
            "addr": self.addr,
            "port": self.port,
            "certificate": self.cert.as_dict() if self.cert else None,
            "enabled": self.enabled
        }


def get(id=None, type=None, verify=True):
    data = storage.sites.get("sites")
    if not data:
        data = scan()
    if id or type:
        tlist = []
        for x in data:
            if x.id == id:
                return x
            elif x.meta.id == type:
                tlist.append(x)
        if tlist:
            return tlist
        return None
    return data

def scan():
    sites = []
    for site in os.listdir('/etc/nginx/sites-available'):
        path = os.path.join('/etc/nginx/sites-available', site)
        if not os.path.exists(path):
            continue
        rport = re.compile('(\\d+)\s*(.*)')
        g = ConfigParser.SafeConfigParser()
        if not g.read(os.path.join(path, ".arkos")):
            continue
        stype = g.get('website', 'type')
        if stype != "ReverseProxy":
            cls = applications.get(stype)
            s = cls._website(name=g.get('website', 'name'))
            s.meta = cls
            s.backup = cls.get_module("backup") or backup.BackupController
            s.backup = s.backup(s.name, s)
            s.data_path = c.get("website", "data_path", "")
            if s.data_path:
                filesystems.register_point(g.get('website', 'name'), s.data_path, 
                    "site", cls.icon if cls else "gen-earth")
        else:
            s = ReverseProxy(name=g.get('website', 'name'))
            s.name = g.get("website", "pretty_name")
            s.type = g.get("website", "extra")
            s.meta = None
            s.backup = None
        try:
            ssl = None
            c = nginx.loadf(path)
            # Get the right serverblock - SSL if it's here
            for x in c.servers:
                if 'ssl' in x.filter('Key', 'listen')[0].value:
                    s.ssl = True
                    n = x
                    break
            else:
                n = c.servers[0]
            s.port, ssl = re.match(rport, n.filter('Key', 'listen')[0].value).group(1, 2)
            if ssl:
                s.cert = certificates.get(os.path.splitext(os.path.split(n.filter('Key', 'ssl_certificate')[0].value)[1])[0])
                s.cert.assign.append({"type": "website", "name": s.name})
            s.port = int(s.port)
            s.addr = n.filter('Key', 'server_name')[0].value
            s.path = n.filter('Key', 'root')[0].value
            s.php = 'php' in n.filter('Key', 'index')[0].value
        except IndexError:
            pass
        s.version = g.get('website', 'version', None)
        if g.get('website', 'dbengine', None):
            s.db = databases.get(g.get("website", "dbname"))
        s.enabled = True if os.path.exists(os.path.join('/etc/nginx/sites-enabled', g.get('website', 'name'))) else False
        s.installed = True
        sites.append(s)
        tracked_services.register(s.meta.id if s.meta else "website", s.name, 
            s.pretty_name if hasattr(s, "pretty_name") and s.pretty_name else s.name, 
            s.meta.icon if s.meta else "gen-earth", [("tcp", s.port)])
    storage.sites.set("sites", sites)
    return sites

def nginx_reload():
    status = shell('systemctl restart nginx')
    if status["code"] >= 1:
        raise ReloadError('nginx failed to reload.', "Edit")

def php_enable():
    shell('sed -i "s/.*include \/etc\/nginx\/php.conf.*/\tinclude \/etc\/nginx\/php.conf;/" /etc/nginx/nginx.conf')

def php_disable():
    shell('sed -i "s/.*include \/etc\/nginx\/php.conf.*/\t#include \/etc\/nginx\/php.conf;/" /etc/nginx/nginx.conf')

def php_reload():
    status = shell('systemctl restart php-fpm')
    if status["code"] >= 1:
        raise Exception('PHP FastCGI failed to reload.')

def get_new_id():
    return max([x.id for x in get()]) + 1
