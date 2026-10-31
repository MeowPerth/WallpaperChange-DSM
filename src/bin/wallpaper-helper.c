/*
 * wallpaper-helper — small setuid-root helper for the Wallpaper Changer SPK.
 *
 * DSM 7.4 refuses `run-as: root` for unsigned third-party packages, so the
 * package itself runs as sc-autowallpaper and DSM grants this helper
 * root + setuid via conf/privilege's `tool` section (mode 6550, group
 * autowallpaper).  The helper only supports fixed operations on fixed paths:
 *
 *   start    exec the daemon as root
 *   stop     SIGTERM (then SIGKILL) the verified daemon PID
 *   status   exit 0 if the verified daemon is alive, 3 otherwise
 *   restore  exec `wallpaper-daemon.py --restore` as root (uninstall)
 *   cleanup  remove the pid/socket files
 *
 * It clears the environment, ignores argv beyond the mode, and verifies
 * /proc/<pid>/cmdline before signalling, so it cannot be used as a generic
 * root shell.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef PKG_DIR
#define PKG_DIR "/var/packages/autowallpaper"
#endif
#ifndef SOCKET_PATH
#define SOCKET_PATH "/var/run/autowallpaper.sock"
#endif

#define DAEMON      PKG_DIR "/target/bin/wallpaper-daemon.py"
#define VARDIR      PKG_DIR "/var"
#define PIDFILE     VARDIR "/wallpaper.pid"
#define DAEMON_NAME "wallpaper-daemon.py"

static const char *python_candidates[] = {
    "/usr/bin/python3",
    "/usr/local/bin/python3",
    "/var/packages/Python3.9/target/bin/python3",
    "/var/packages/Python3.8/target/bin/python3",
    NULL
};

static void usage(void)
{
    fprintf(stderr, "usage: wallpaper-helper start|stop|status|restore|cleanup\n");
}

static int make_root(void)
{
    if (geteuid() != 0) {
        fprintf(stderr, "wallpaper-helper: not running setuid root\n");
        return -1;
    }
    if (setgroups(0, NULL) != 0) {
        perror("setgroups");
        return -1;
    }
    if (setgid(0) != 0) {
        perror("setgid");
        return -1;
    }
    if (setuid(0) != 0) {
        perror("setuid");
        return -1;
    }
    if (geteuid() != 0 || getuid() != 0) {
        fprintf(stderr, "wallpaper-helper: failed to become root\n");
        return -1;
    }
    return 0;
}

static void setup_env(void)
{
    clearenv();
    setenv("PATH", "/usr/syno/bin:/usr/bin:/bin:/usr/syno/sbin:/usr/sbin:/sbin", 1);
    setenv("HOME", "/root", 1);
    setenv("SYNOPKG_PKGVAR", VARDIR, 1);
    setenv("PYTHONUNBUFFERED", "1", 1);
    setenv("LANG", "C.UTF-8", 1);
}

static const char *find_python(void)
{
    int i;
    for (i = 0; python_candidates[i] != NULL; i++) {
        if (access(python_candidates[i], X_OK) == 0)
            return python_candidates[i];
    }
    return NULL;
}

static int read_pid(void)
{
    FILE *f;
    long pid = 0;

    f = fopen(PIDFILE, "r");
    if (!f)
        return 0;
    if (fscanf(f, "%ld", &pid) != 1)
        pid = 0;
    fclose(f);
    if (pid < 2 || pid > 2147483647L)
        return 0;
    return (int)pid;
}

static int is_our_daemon(int pid)
{
    char path[64];
    char buf[8192];
    ssize_t n;
    int fd;

    snprintf(path, sizeof(path), "/proc/%d/cmdline", pid);
    fd = open(path, O_RDONLY);
    if (fd < 0)
        return 0;
    n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0)
        return 0;
    {
        ssize_t i;
        for (i = 0; i < n; i++) {
            if (buf[i] == '\0')
                buf[i] = ' ';
        }
    }
    buf[n] = '\0';
    return strstr(buf, DAEMON_NAME) != NULL;
}

static int pid_alive(int pid)
{
    return kill(pid, 0) == 0;
}

static int running_pid(void)
{
    int pid = read_pid();
    if (pid <= 0)
        return 0;
    if (!pid_alive(pid))
        return 0;
    if (!is_our_daemon(pid))
        return 0;
    return pid;
}

static void cleanup_files(void)
{
    unlink(PIDFILE);
    unlink(SOCKET_PATH);
}

static int do_status(void)
{
    int pid = running_pid();
    if (pid) {
        printf("running (pid %d)\n", pid);
        return 0;
    }
    cleanup_files();
    printf("not running\n");
    return 3;
}

static int do_stop(void)
{
    int pid = running_pid();
    int i;

    if (!pid) {
        cleanup_files();
        return 0;
    }
    if (kill(pid, SIGTERM) != 0 && errno != ESRCH) {
        perror("kill(SIGTERM)");
        return 1;
    }
    for (i = 0; i < 100; i++) {
        if (!pid_alive(pid))
            break;
        usleep(200000);
    }
    if (pid_alive(pid)) {
        kill(pid, SIGKILL);
        for (i = 0; i < 20; i++) {
            if (!pid_alive(pid))
                break;
            usleep(100000);
        }
    }
    cleanup_files();
    return 0;
}

static int do_start(void)
{
    const char *python;
    int pid = running_pid();

    if (pid) {
        fprintf(stderr, "wallpaper-daemon already running (pid %d)\n", pid);
        return 1;
    }
    python = find_python();
    if (!python) {
        fprintf(stderr, "python3 not found\n");
        return 1;
    }
    if (access(DAEMON, R_OK) != 0) {
        fprintf(stderr, "daemon not found: %s\n", DAEMON);
        return 1;
    }
    setup_env();
    execl(python, "python3", DAEMON, "--no-console-log", (char *)NULL);
    perror("execl daemon");
    return 1;
}

static int do_restore(void)
{
    const char *python = find_python();
    int pid = running_pid();

    if (pid) {
        fprintf(stderr, "refusing to restore while daemon is running (pid %d)\n", pid);
        return 1;
    }
    if (!python) {
        fprintf(stderr, "python3 not found\n");
        return 1;
    }
    if (access(DAEMON, R_OK) != 0) {
        fprintf(stderr, "daemon not found: %s\n", DAEMON);
        return 1;
    }
    setup_env();
    execl(python, "python3", DAEMON, "--restore", (char *)NULL);
    perror("execl restore");
    return 1;
}

int main(int argc, char **argv)
{
    umask(022);
    if (make_root() != 0)
        return 1;
    if (chdir("/") != 0)
        return 1;
    if (argc != 2) {
        usage();
        return 2;
    }
    if (strcmp(argv[1], "status") == 0)
        return do_status();
    if (strcmp(argv[1], "stop") == 0)
        return do_stop();
    if (strcmp(argv[1], "start") == 0)
        return do_start();
    if (strcmp(argv[1], "restore") == 0)
        return do_restore();
    if (strcmp(argv[1], "cleanup") == 0) {
        cleanup_files();
        return 0;
    }
    usage();
    return 2;
}
