#!/usr/bin/python

"""
This overwrites the system wide torrc, located at /etc/tor/torrc, with the
contents of the ARM_CONFIG_FILE. The system wide torrc is owned by root so
this will effectively need root permissions and for us to be in GROUP.

This file is completely unusable until we make a tor-arm user and perform
other prep by running with the '--init' argument.

After that's done this is meant to either be used by arm automatically after
writing a torrc to ARM_CONFIG_FILE or by users manually. For arm to use this
automatically you'll either need to...

- compile torrc-override.c, setuid on the binary, and move it to your path
  cd /usr/share/arm/resources/torrc-override
  make
  chown root:tor-arm override
  chmod 04750 override
  mv override /usr/bin/torrc-override

- allow passwordless sudo for this script
  edit /etc/sudoers and add a line with:
  <arm user> ALL= NOPASSWD: /usr/share/arm/resources/torrc-override/override.py

To perform this manually run:
/usr/share/arm/resources/torrc-override/override.py
pkill -sighup tor
"""

import os
import sys
import grp
import pwd
import time
import shutil
import tempfile
import signal

USER = "tor-arm"
GROUP = "tor-arm"
TOR_CONFIG_FILE = "/etc/tor/torrc"
ARM_CONFIG_FILE = "/var/lib/tor-arm/torrc"

HELP_MSG = """Usage %s [OPTION]
  Backup the system wide torrc (%s) and replace it with the
  contents of %s.

  --init    creates the necessary user and paths
  --remove  reverts changes made with --init
""" % (os.path.basename(sys.argv[0]), TOR_CONFIG_FILE, ARM_CONFIG_FILE)

def init():
  """
  Performs system preparation needed for this script to run, adding the tor-arm
  user and setting up paths/permissions.
  """
  
  # the following is just here if we have a custom destination directory (which
  # arm doesn't currently account for)
  if not os.path.exists("/bin/"):
    print "making '/bin'..."
    os.mkdir("/bin")
  
  if not os.path.exists("/var/lib/tor-arm/"):
    print "making '/var/lib/tor-arm'..."
    os.makedirs("/var/lib/tor-arm")
  
  if not os.path.exists("/var/lib/tor-arm/torrc"):
    print "making '/var/lib/tor-arm/torrc'..."
    open("/var/lib/tor-arm/torrc", 'w').close()
  
  try: gid = grp.getgrnam(GROUP).gr_gid
  except KeyError:
    print "adding %s group..." % GROUP
    os.system("addgroup --quiet --system %s" % GROUP)
    gid = grp.getgrnam(GROUP).gr_gid
    print "  done, gid: %s" % gid
  
  try: pwd.getpwnam(USER).pw_uid
  except KeyError:
    print "adding %s user..." % USER
    os.system("adduser --quiet --ingroup %s --no-create-home --home /var/lib/tor-arm/ --shell /bin/sh --system %s" % (GROUP, USER))
    uid = pwd.getpwnam(USER).pw_uid
    print "  done, uid: %s" % uid
  
  os.chown("/bin", 0, 0)
  os.chown("/var/lib/tor-arm", 0, gid)
  os.chmod("/var/lib/tor-arm", 0750)
  os.chown("/var/lib/tor-arm/torrc", 0, gid)
  os.chmod("/var/lib/tor-arm/torrc", 0760)

def remove():
  """
  Reverts the changes made by init, and also removes the optional
  /bin/torrc-override binary if it exists.
  """
  
  print "removing %s user..." % USER
  os.system("deluser --quiet %s" % USER)
  
  print "removing %s group..." % GROUP
  os.system("delgroup --quiet %s" % GROUP)
  
  # might not exist since this is compiled and placed separately
  try:
    print "removing '/bin/torrc-override'..."
    os.remove("/bin/torrc-override")
  except OSError:
    print "  no such path"
  
  try:
    print "removing '/var/lib/tor-arm'..."
    shutil.rmtree("/var/lib/tor-arm/")
  except Exception, exc:
    print "  unsuccessful: %s" % exc

def replaceTorrc():
  orig_uid = os.getuid()
  orig_euid = os.geteuid()
  
  # the USER and GROUP must exist on this system
  try:
    dropped_uid = pwd.getpwnam(USER).pw_uid
    dropped_gid = grp.getgrnam(GROUP).gr_gid
    dropped_euid, dropped_egid = dropped_uid, dropped_gid
  except KeyError:
    print "tor-arm user and group was not found, have you run this script with '--init'?"
    exit(1)
  
  # if we're actually root, we skip this group check
  # root can get away with all of this
  if orig_uid != 0:
    # check that the user is in GROUP
    if not dropped_gid in os.getgroups():
      print "Your user needs to be a member of the %s group for this to work" % GROUP
      sys.exit(1)
  
  # drop to the unprivileged group, and lose the rest of the groups
  os.setgid(dropped_gid)
  os.setegid(dropped_egid)
  os.setresgid(dropped_gid, dropped_egid, dropped_gid)
  os.setgroups([dropped_gid])
  
  # make a tempfile and write out the contents
  try:
    tf = tempfile.NamedTemporaryFile(delete=False) # uses mkstemp internally
    
    # allows our child process to write to tf.name (not only if their uid matches, not their gid) 
    os.chown(tf.name, dropped_uid, orig_euid)
  except:
    print "We were unable to make a temporary file"
    sys.exit(1)
  
  fork_pid = os.fork()
  
  # open the suspect config after we drop privs
  # we assume the dropped privs are still enough to write to the tf
  if (fork_pid == 0):
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    
    # Drop privs forever in the child process
    # I believe this drops os.setfsuid os.setfsgid stuff
    # Clear all other supplemental groups for dropped_uid
    os.setgroups([dropped_gid])
    os.setresgid(dropped_gid, dropped_egid, dropped_gid)
    os.setresuid(dropped_uid, dropped_euid, dropped_uid)
    os.setgid(dropped_gid)
    os.setegid(dropped_egid)
    os.setuid(dropped_uid)
    os.seteuid(dropped_euid)
    
    try:
      af = open(ARM_CONFIG_FILE) # this is totally unpriv'ed
      
      # ensure that the fd we opened has the properties we requrie
      configStat = os.fstat(af.fileno()) # this happens on the unpriv'ed FD
      if configStat.st_gid != dropped_gid:
        print "Arm's configuration file (%s) must be owned by the group %s" % (ARM_CONFIG_FILE, GROUP)
        sys.exit(1)
      
      # if everything checks out, we're as safe as we're going to get
      armConfig = af.read(1024 * 1024) # limited read but not too limited
      af.close()
      tf.file.write(armConfig)
      tf.flush()
    except:
      print "Unable to open the arm config as unpriv'ed user"
      sys.exit(1)
    finally:
      tf.close()
      sys.exit(0)
  else:
    # If we're here, we're in the parent waiting for the child's death
    # man, unix is really weird...
    _, status = os.waitpid(fork_pid, 0)
  
  if status != 0:
    print "The child seems to have failed; exiting!"
    tf.close()
    sys.exit(1)
  
  # attempt to verify that the config is OK
  if os.path.exists(tf.name):
    # construct our SU string
    SUSTRING = "su -c 'tor --verify-config -f " + str(tf.name) + "' " + USER
    # We raise privs to drop them with 'su'
    os.setuid(0)
    os.seteuid(0)
    os.setgid(0)
    os.setegid(0)
    # We drop privs here and exec tor to verify it as the dropped_uid 
    print "Using Tor to verify that arm will not break Tor's config:"
    success = os.system(SUSTRING)
    if success != 0:
      print "Tor says the new configuration file is invalid: %s (%s)" % (ARM_CONFIG_FILE, tf.name)
      sys.exit(1)
  
  # backup the previous tor config
  if os.path.exists(TOR_CONFIG_FILE):
    try:
      backupFilename = "%s_backup_%i" % (TOR_CONFIG_FILE, int(time.time()))
      shutil.copy(TOR_CONFIG_FILE, backupFilename)
    except IOError, exc:
      print "Unable to backup %s (%s)" % (TOR_CONFIG_FILE, exc)
      sys.exit(1)
  
  # overwrites TOR_CONFIG_FILE with ARM_CONFIG_FILE as loaded into tf.name
  try:
    shutil.copy(tf.name, TOR_CONFIG_FILE)
    print "Successfully reconfigured Tor"
  except IOError, exc:
    print "Unable to copy %s to %s (%s)" % (tf.name, TOR_CONFIG_FILE, exc)
    sys.exit(1)
  
  # unlink our temp file
  try:
    os.remove(tf.name)
  except:
    print "Unable to close temp file %s" % tf.name
    sys.exit(1)
  
  sys.exit(0)

if __name__ == "__main__":
  # sanity check that we're on linux
  if os.name != "posix":
    print "This is a script specifically for configuring Linux"
    sys.exit(1)
  
  # check that we're running effectively as root
  if os.geteuid() != 0:
    print "This script needs to be run as root"
    sys.exit(1)
  
  if len(sys.argv) < 2:
    replaceTorrc()
  elif len(sys.argv) == 2 and sys.argv[1] == "--init":
    init()
  elif len(sys.argv) == 2 and sys.argv[1] == "--remove":
    remove()
  else:
    print HELP_MSG
    sys.exit(1)

