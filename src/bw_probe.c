/*
 * bw_probe — persistent UCX UCP bandwidth probe agent
 *
 * Protocol (newline-delimited text over TCP control socket):
 *   CONNECT <peer_id> <peer_ip> <oob_port>  → OK CONNECTED | ERR ...
 *   MEASURE <peer_id>                        → OK <bw_MBps> <cksum_ok>
 *   RESPOND <peer_id>                        → OK 0
 *   QUIT                                     → OK BYE
 *
 * MEASURE (source): send 64 MiB → recv 64 MiB echo, report BW + checksum
 * RESPOND (target): recv 64 MiB → send 64 MiB echo
 * Tag scheme: tag = (sender_id << 32) | receiver_id
 */

#include <ucp/api/ucp.h>
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define MAX_PEERS  16
#define XFER_SIZE  (64UL * 1024 * 1024)

static volatile int g_running = 1;

typedef struct { volatile int done; ucs_status_t status; } req_ctx_t;

static void req_init(void *r) {
    req_ctx_t *c = (req_ctx_t *)r;
    c->done = 0;
    c->status = UCS_INPROGRESS;
}
static void send_cb(void *r, ucs_status_t s, void *u) {
    req_ctx_t *c = (req_ctx_t *)r;
    c->status = s;
    c->done = 1;
}
static void recv_cb(void *r, ucs_status_t s, const ucp_tag_recv_info_t *i, void *u) {
    req_ctx_t *c = (req_ctx_t *)r;
    c->status = s;
    c->done = 1;
}

static ucs_status_t wait_req(ucp_worker_h w, void *r) {
    if (r == NULL) return UCS_OK;
    if (UCS_PTR_IS_ERR(r)) return UCS_PTR_STATUS(r);
    req_ctx_t *c = (req_ctx_t *)r;
    while (!c->done)
        ucp_worker_progress(w);
    ucs_status_t s = c->status;
    ucp_request_free(r);
    return s;
}

static uint64_t cksum64(const void *buf, size_t len) {
    const uint64_t *p = buf;
    uint64_t h = 0xcbf29ce484222325ULL;
    for (size_t i = 0; i < len / 8; i++)
        h ^= p[i], h *= 0x100000001b3ULL;
    return h;
}

typedef struct { int id; int active; ucp_ep_h ep; } peer_t;

typedef struct {
    ucp_context_h  ctx;
    ucp_worker_h   worker;
    ucp_address_t *my_addr;
    size_t         my_addr_len;
    peer_t         peers[MAX_PEERS];
    int            n_peers;
    void          *buf;
    int            my_id;
} agent_t;

static int tcp_listen_on(int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    int opt = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    struct sockaddr_in a = {.sin_family=AF_INET, .sin_port=htons(port), .sin_addr.s_addr=INADDR_ANY};
    if (bind(fd, (void*)&a, sizeof(a)) < 0) { perror("bind"); close(fd); return -1; }
    listen(fd, 4);
    return fd;
}

static int tcp_connect_to(const char *ip, int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a = {.sin_family=AF_INET, .sin_port=htons(port)};
    inet_pton(AF_INET, ip, &a.sin_addr);
    for (int i = 0; i < 50; i++) {
        if (connect(fd, (void*)&a, sizeof(a)) == 0) return fd;
        usleep(100000);
    }
    close(fd);
    return -1;
}

static int xfer_bytes(int fd, void *buf, size_t len, int do_send) {
    char *p = buf;
    while (len > 0) {
        ssize_t n = do_send ? send(fd, p, len, 0) : recv(fd, p, len, MSG_WAITALL);
        if (n <= 0) return -1;
        p += n; len -= n;
    }
    return 0;
}

static int agent_init(agent_t *a) {
    ucp_config_t *cfg;
    ucp_config_read(NULL, NULL, &cfg);
    ucp_params_t p = {
        .field_mask   = UCP_PARAM_FIELD_FEATURES | UCP_PARAM_FIELD_REQUEST_SIZE |
                        UCP_PARAM_FIELD_REQUEST_INIT | UCP_PARAM_FIELD_NAME,
        .features     = UCP_FEATURE_TAG,
        .request_size = sizeof(req_ctx_t),
        .request_init = req_init,
        .name         = "bw_probe",
    };
    ucs_status_t s = ucp_init(&p, cfg, &a->ctx);
    ucp_config_release(cfg);
    if (s != UCS_OK) return -1;

    ucp_worker_params_t wp = {
        .field_mask  = UCP_WORKER_PARAM_FIELD_THREAD_MODE,
        .thread_mode = UCS_THREAD_MODE_SINGLE,
    };
    s = ucp_worker_create(a->ctx, &wp, &a->worker);
    if (s != UCS_OK) return -1;

    ucp_worker_attr_t wa = { .field_mask = UCP_WORKER_ATTR_FIELD_ADDRESS };
    ucp_worker_query(a->worker, &wa);
    a->my_addr     = wa.address;
    a->my_addr_len = wa.address_length;
    a->buf = calloc(1, XFER_SIZE);
    return 0;
}

/* OOB TCP handshake: lower id dials, higher id accepts */
static int agent_connect(agent_t *a, int peer_id, const char *peer_ip, int oob_port) {
    if (a->n_peers >= MAX_PEERS) return -1;
    peer_t *pr = &a->peers[a->n_peers];
    pr->id = peer_id;

    ucp_address_t *raddr = NULL;
    uint64_t raddr_len = 0;

    if (a->my_id < peer_id) {
        int oob = tcp_connect_to(peer_ip, oob_port);
        if (oob < 0) return -1;
        uint64_t len = a->my_addr_len;
        xfer_bytes(oob, &len, 8, 1);
        xfer_bytes(oob, a->my_addr, a->my_addr_len, 1);
        xfer_bytes(oob, &raddr_len, 8, 0);
        raddr = malloc(raddr_len);
        xfer_bytes(oob, raddr, raddr_len, 0);
        close(oob);
    } else {
        int lfd = tcp_listen_on(oob_port);
        if (lfd < 0) return -1;
        int oob = accept(lfd, NULL, NULL);
        close(lfd);
        if (oob < 0) return -1;
        xfer_bytes(oob, &raddr_len, 8, 0);
        raddr = malloc(raddr_len);
        xfer_bytes(oob, raddr, raddr_len, 0);
        uint64_t len = a->my_addr_len;
        xfer_bytes(oob, &len, 8, 1);
        xfer_bytes(oob, a->my_addr, a->my_addr_len, 1);
        close(oob);
    }

    ucp_ep_params_t ep = { .field_mask = UCP_EP_PARAM_FIELD_REMOTE_ADDRESS, .address = raddr };
    ucs_status_t s = ucp_ep_create(a->worker, &ep, &pr->ep);
    free(raddr);
    if (s != UCS_OK) return -1;
    pr->active = 1;
    a->n_peers++;
    fprintf(stderr, "[agent %d] connected to peer %d\n", a->my_id, peer_id);
    return 0;
}

static peer_t *find_peer(agent_t *a, int id) {
    for (int i = 0; i < a->n_peers; i++)
        if (a->peers[i].id == id && a->peers[i].active) return &a->peers[i];
    return NULL;
}

static double agent_measure(agent_t *a, int peer_id, int *cksum_ok) {
    peer_t *pr = find_peer(a, peer_id);
    if (!pr) { fprintf(stderr, "[m] peer %d not found\n", peer_id); *cksum_ok = 0; return -1; }

    uint64_t *p = (uint64_t *)a->buf;
    for (size_t i = 0; i < XFER_SIZE / 8; i++)
        p[i] = i ^ 0xdeadbeefcafeULL;
    uint64_t expect = cksum64(a->buf, XFER_SIZE);

    ucp_tag_t tsend = ((uint64_t)a->my_id << 32) | peer_id;
    ucp_tag_t trecv = ((uint64_t)peer_id << 32) | a->my_id;

    ucp_request_param_t sp = { .op_attr_mask = UCP_OP_ATTR_FIELD_CALLBACK, .cb.send = send_cb };
    ucp_request_param_t rp = { .op_attr_mask = UCP_OP_ATTR_FIELD_CALLBACK |
                                UCP_OP_ATTR_FIELD_DATATYPE | UCP_OP_ATTR_FLAG_NO_IMM_CMPL,
                                .datatype = ucp_dt_make_contig(1), .cb.recv = recv_cb };

    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);

    void *sr = ucp_tag_send_nbx(pr->ep, a->buf, XFER_SIZE, tsend, &sp);
    if (wait_req(a->worker, sr) != UCS_OK) { *cksum_ok = 0; return -1; }

    void *rr = ucp_tag_recv_nbx(a->worker, a->buf, XFER_SIZE, trecv, UINT64_MAX, &rp);
    if (wait_req(a->worker, rr) != UCS_OK) { *cksum_ok = 0; return -1; }

    clock_gettime(CLOCK_MONOTONIC, &t1);

    *cksum_ok = (cksum64(a->buf, XFER_SIZE) == expect) ? 1 : 0;
    double elapsed = (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) * 1e-9;
    /* per-direction BW: total 128 MiB round-trip, report one-way equivalent */
    return ((double)XFER_SIZE / (1024.0 * 1024.0)) / elapsed;
}

static int agent_respond(agent_t *a, int peer_id) {
    peer_t *pr = find_peer(a, peer_id);
    if (!pr) return -1;

    ucp_tag_t trecv = ((uint64_t)peer_id << 32) | a->my_id;
    ucp_tag_t tsend = ((uint64_t)a->my_id << 32) | peer_id;

    ucp_request_param_t rp = { .op_attr_mask = UCP_OP_ATTR_FIELD_CALLBACK |
                                UCP_OP_ATTR_FIELD_DATATYPE | UCP_OP_ATTR_FLAG_NO_IMM_CMPL,
                                .datatype = ucp_dt_make_contig(1), .cb.recv = recv_cb };
    ucp_request_param_t sp = { .op_attr_mask = UCP_OP_ATTR_FIELD_CALLBACK, .cb.send = send_cb };

    void *rr = ucp_tag_recv_nbx(a->worker, a->buf, XFER_SIZE, trecv, UINT64_MAX, &rp);
    if (wait_req(a->worker, rr) != UCS_OK) return -1;

    void *sr = ucp_tag_send_nbx(pr->ep, a->buf, XFER_SIZE, tsend, &sp);
    if (wait_req(a->worker, sr) != UCS_OK) return -1;

    return 0;
}

static void ctrl_loop(agent_t *a, int fd) {
    FILE *f = fdopen(fd, "r+");
    setvbuf(f, NULL, _IOLBF, 0);
    char line[256];

    while (g_running && fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\r\n")] = 0;

        if (strncmp(line, "CONNECT ", 8) == 0) {
            int pid, oob; char pip[64];
            if (sscanf(line+8, "%d %63s %d", &pid, pip, &oob) == 3)
                fprintf(f, agent_connect(a, pid, pip, oob) == 0 ? "OK CONNECTED\n" : "ERR\n");
            else fprintf(f, "ERR bad_args\n");
        } else if (strncmp(line, "MEASURE ", 8) == 0) {
            int pid, ck;
            if (sscanf(line+8, "%d", &pid) == 1) {
                double bw = agent_measure(a, pid, &ck);
                fprintf(f, "OK %.2f %d\n", bw, ck);
            } else fprintf(f, "ERR\n");
        } else if (strncmp(line, "RESPOND ", 8) == 0) {
            int pid;
            if (sscanf(line+8, "%d", &pid) == 1) {
                agent_respond(a, pid);
                fprintf(f, "OK 0\n");
            } else fprintf(f, "ERR\n");
        } else if (strcmp(line, "QUIT") == 0) {
            fprintf(f, "OK BYE\n"); break;
        }
    }
    fclose(f);
}

static void sighandler(int s) { g_running = 0; }

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "Usage: %s <my_id> <ctrl_port>\n", argv[0]); return 1; }
    signal(SIGINT, sighandler); signal(SIGTERM, sighandler); signal(SIGPIPE, SIG_IGN);

    agent_t a; memset(&a, 0, sizeof(a));
    a.my_id = atoi(argv[1]);
    int ctrl_port = atoi(argv[2]);

    if (agent_init(&a) != 0) { fprintf(stderr, "UCP init failed\n"); return 1; }
    fprintf(stderr, "[agent %d] UCP ready, addr_len=%zu\n", a.my_id, a.my_addr_len);

    int lfd = tcp_listen_on(ctrl_port);
    if (lfd < 0) { fprintf(stderr, "listen %d failed\n", ctrl_port); return 1; }
    fprintf(stderr, "[agent %d] ctrl on port %d\n", a.my_id, ctrl_port);

    while (g_running) {
        int cfd = accept(lfd, NULL, NULL);
        if (cfd < 0) continue;
        int opt = 1; setsockopt(cfd, IPPROTO_TCP, TCP_NODELAY, &opt, sizeof(opt));
        fprintf(stderr, "[agent %d] ctrl connected\n", a.my_id);
        ctrl_loop(&a, cfd);
    }

    close(lfd);
    for (int i = 0; i < a.n_peers; i++)
        if (a.peers[i].active) {
            ucp_request_param_t p = { .op_attr_mask = 0 };
            wait_req(a.worker, ucp_ep_close_nbx(a.peers[i].ep, &p));
        }
    free(a.buf);
    ucp_worker_release_address(a.worker, a.my_addr);
    ucp_worker_destroy(a.worker);
    ucp_cleanup(a.ctx);
    return 0;
}
