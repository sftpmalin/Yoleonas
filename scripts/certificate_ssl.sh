#!/usr/bin/env bash
set -Eeuo pipefail

# Renouvelle les certificats Let's Encrypt de Nginx Proxy Manager via Docker.
# Par defaut, renouvelle les certificats expires ou qui expirent dans 30 jours.

NPM_CONTAINER="${NPM_CONTAINER:-nginx_proxy_manager}"
NPM_WEB_URL="${NPM_WEB_URL:-http://192.168.2.81:81}"
RENEW_BEFORE_DAYS="${RENEW_BEFORE_DAYS:-30}"
NPM_TEMP_DISABLE_HOSTS="${NPM_TEMP_DISABLE_HOSTS:-1}"
CERTBOT_LOCK_RETRIES="${CERTBOT_LOCK_RETRIES:-12}"
CERTBOT_LOCK_WAIT_SECONDS="${CERTBOT_LOCK_WAIT_SECONDS:-10}"
LOG_DIR="${LOG_DIR:-/var/log/yoleo}"
LOCK_FILE="${LOCK_FILE:-/tmp/yoleo-npm-ssl-renew.lock}"

DRY_RUN=0
LIST_ONLY=0
FORCE=0

usage() {
    cat <<'USAGE'
Usage: certificate_ssl.sh [options]

Options:
  --list              Liste les certificats et ce que le script ferait.
  --dry-run           Identique a --list, sans renouveler.
  --force             Renouvelle tous les certificats Let's Encrypt actifs.
  --days N            Renouvelle si expiration dans N jours (defaut: 30).
                      Mets 0 pour renouveler seulement les certificats expires.
  --container NOM     Nom du conteneur Nginx Proxy Manager.
  --no-temp-disable   Ne retire pas temporairement les configs Nginx des hosts.
  -h, --help          Affiche cette aide.

Variables utiles:
  NPM_CONTAINER=nginx_proxy_manager
  NPM_WEB_URL=http://192.168.2.81:81
  RENEW_BEFORE_DAYS=30
  NPM_TEMP_DISABLE_HOSTS=1
  CERTBOT_LOCK_RETRIES=12
  CERTBOT_LOCK_WAIT_SECONDS=10
USAGE
}

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
    log "ERREUR: $*"
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --list)
            LIST_ONLY=1
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --force)
            FORCE=1
            shift
            ;;
        --days)
            [ "$#" -ge 2 ] || die "option --days sans valeur"
            RENEW_BEFORE_DAYS="$2"
            shift 2
            ;;
        --container)
            [ "$#" -ge 2 ] || die "option --container sans valeur"
            NPM_CONTAINER="$2"
            shift 2
            ;;
        --no-temp-disable)
            NPM_TEMP_DISABLE_HOSTS=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "option inconnue: $1"
            ;;
    esac
done

case "$RENEW_BEFORE_DAYS" in
    ''|*[!0-9]*)
        die "RENEW_BEFORE_DAYS doit etre un nombre entier positif ou 0"
        ;;
esac

for number_var in CERTBOT_LOCK_RETRIES CERTBOT_LOCK_WAIT_SECONDS; do
    value="${!number_var}"
    case "$value" in
        ''|*[!0-9]*)
            die "$number_var doit etre un nombre entier positif ou 0"
            ;;
    esac
done

if ! command -v docker >/dev/null 2>&1; then
    die "docker est introuvable"
fi

if ! mkdir -p "$LOG_DIR" 2>/dev/null; then
    LOG_DIR="/tmp"
fi
LOG_FILE="$LOG_DIR/npm-ssl-renew.log"
touch "$LOG_FILE" 2>/dev/null || LOG_FILE="/tmp/npm-ssl-renew.log"
exec > >(tee -a "$LOG_FILE") 2>&1

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    die "un renouvellement est deja en cours ($LOCK_FILE)"
fi

if [ "$(docker inspect -f '{{.State.Running}}' "$NPM_CONTAINER" 2>/dev/null || true)" != "true" ]; then
    die "le conteneur '$NPM_CONTAINER' n'est pas lance"
fi

log "Nginx Proxy Manager: $NPM_CONTAINER ($NPM_WEB_URL)"
log "Seuil de renouvellement: $RENEW_BEFORE_DAYS jour(s)"
log "Mode temporaire ACME: $NPM_TEMP_DISABLE_HOSTS"
log "Log: $LOG_FILE"

if docker exec -i \
    --env RENEW_BEFORE_DAYS="$RENEW_BEFORE_DAYS" \
    --env DRY_RUN="$DRY_RUN" \
    --env LIST_ONLY="$LIST_ONLY" \
    --env FORCE="$FORCE" \
    --env NPM_TEMP_DISABLE_HOSTS="$NPM_TEMP_DISABLE_HOSTS" \
    --env CERTBOT_LOCK_RETRIES="$CERTBOT_LOCK_RETRIES" \
    --env CERTBOT_LOCK_WAIT_SECONDS="$CERTBOT_LOCK_WAIT_SECONDS" \
    "$NPM_CONTAINER" node --input-type=module <<'NODE'
import fs from "node:fs";
import path from "node:path";
import { execFile as execFileCallback } from "node:child_process";
import { promisify } from "node:util";

import internalCertificate from "/app/internal/certificate.js";
import internalHost from "/app/internal/host.js";
import internalNginx from "/app/internal/nginx.js";
import certificateModel from "/app/models/certificate.js";
import tokenModel from "/app/models/token.js";

const execFile = promisify(execFileCallback);

const options = {
	renewBeforeDays: Number.parseInt(process.env.RENEW_BEFORE_DAYS || "30", 10),
	dryRun: process.env.DRY_RUN === "1",
	listOnly: process.env.LIST_ONLY === "1",
	force: process.env.FORCE === "1",
	tempDisableHosts: process.env.NPM_TEMP_DISABLE_HOSTS !== "0",
	certbotLockRetries: Number.parseInt(process.env.CERTBOT_LOCK_RETRIES || "12", 10),
	certbotLockWaitSeconds: Number.parseInt(process.env.CERTBOT_LOCK_WAIT_SECONDS || "10", 10),
};

const thresholdSeconds = options.renewBeforeDays * 24 * 60 * 60;
const access = {
	can: async () => ({ permission_visibility: "all" }),
	token: tokenModel(),
};

function log(message) {
	const stamp = new Date().toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC");
	console.log(`[${stamp}] ${message}`);
}

function formatDomains(certificate) {
	if (!Array.isArray(certificate.domain_names)) {
		return String(certificate.domain_names || "");
	}
	return certificate.domain_names.join(", ");
}

function formatDate(date) {
	if (!date) {
		return "inconnue";
	}
	return date.toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC");
}

function formatRemaining(secondsRemaining) {
	if (!Number.isFinite(secondsRemaining)) {
		return "date inconnue";
	}
	if (secondsRemaining <= 0) {
		const days = Math.ceil(Math.abs(secondsRemaining) / 86400);
		return `expire depuis ${days} jour(s)`;
	}
	const days = Math.floor(secondsRemaining / 86400);
	const hours = Math.floor((secondsRemaining % 86400) / 3600);
	return `expire dans ${days} jour(s) ${hours} h`;
}

function getCertPath(certificateId) {
	return `/etc/letsencrypt/live/npm-${certificateId}/fullchain.pem`;
}

async function readCertificateInfo(certificateId) {
	const certPath = getCertPath(certificateId);
	if (!fs.existsSync(certPath)) {
		return {
			exists: false,
			certPath,
			notAfter: null,
			secondsRemaining: Number.NEGATIVE_INFINITY,
		};
	}

	const { stdout } = await execFile("/usr/bin/openssl", ["x509", "-enddate", "-noout", "-in", certPath]);
	const match = stdout.trim().match(/^notAfter=(.+)$/);
	if (!match) {
		throw new Error(`impossible de lire la date du certificat ${certPath}`);
	}

	const notAfter = new Date(match[1]);
	if (Number.isNaN(notAfter.getTime())) {
		throw new Error(`date illisible pour ${certPath}: ${match[1]}`);
	}

	return {
		exists: true,
		certPath,
		notAfter,
		secondsRemaining: Math.floor((notAfter.getTime() - Date.now()) / 1000),
	};
}

function shouldRenew(certInfo) {
	if (options.force) {
		return true;
	}
	if (!certInfo.exists) {
		return true;
	}
	return certInfo.secondsRemaining <= thresholdSeconds;
}

function reasonFor(certInfo) {
	if (options.force) {
		return "force";
	}
	if (!certInfo.exists) {
		return "fichier fullchain.pem manquant";
	}
	if (certInfo.secondsRemaining <= 0) {
		return "certificat expire";
	}
	if (certInfo.secondsRemaining <= thresholdSeconds) {
		return `expiration dans ${options.renewBeforeDays} jour(s) ou moins`;
	}
	return "encore valide";
}

function hostConfigPath(hostType, hostId) {
	return internalNginx.getConfigName(hostType, hostId);
}

function sleep(ms) {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

function isCertbotBusyError(err) {
	const message = String(err && (err.stack || err.message || err) || "");
	return message.includes("Another instance of Certbot is already running");
}

async function prepareHttpChallenge(certificate) {
	const state = {
		backupDir: `/tmp/yoleo-npm-renew-${certificate.id}-${process.pid}-${Date.now()}`,
		backups: [],
		tempGenerated: false,
	};

	try {
		const inUse = await internalHost.getHostsWithDomains(certificate.domain_names);
		const groups = [
			{ type: "proxy_host", hosts: inUse.proxy_hosts || [] },
			{ type: "redirection_host", hosts: inUse.redirection_hosts || [] },
			{ type: "dead_host", hosts: inUse.dead_hosts || [] },
		];

		fs.mkdirSync(state.backupDir, { recursive: true });
		for (const group of groups) {
			for (const host of group.hosts) {
				const source = hostConfigPath(group.type, host.id);
				const dest = path.join(state.backupDir, `${group.type}-${host.id}.conf`);
				if (fs.existsSync(source)) {
					fs.copyFileSync(source, dest);
					fs.unlinkSync(source);
					state.backups.push({ source, dest });
					log(`Host temporairement retire: ${group.type}/${host.id}`);
				}
			}
		}

		await internalNginx.generateLetsEncryptRequestConfig(certificate);
		state.tempGenerated = true;
		await internalNginx.reload();
		return state;
	} catch (err) {
		await restoreHttpChallenge(certificate, state, false);
		throw err;
	}
}

async function restoreHttpChallenge(certificate, state, reload = true) {
	if (!state) {
		return;
	}

	await internalNginx.deleteLetsEncryptRequestConfig(certificate);
	for (const backup of state.backups) {
		fs.mkdirSync(path.dirname(backup.source), { recursive: true });
		fs.copyFileSync(backup.dest, backup.source);
		log(`Host restaure: ${backup.source}`);
	}
	fs.rmSync(state.backupDir, { recursive: true, force: true });

	if (reload) {
		await internalNginx.reload();
	}
}

async function renewCertificate(certificate) {
	const dnsChallenge = Boolean(certificate.meta && certificate.meta.dns_challenge);
	let httpState = null;

	try {
		if (!dnsChallenge && options.tempDisableHosts) {
			httpState = await prepareHttpChallenge(certificate);
		}

		await internalCertificate.renew(access, { id: certificate.id });
		await internalNginx.reload();
	} finally {
		if (httpState) {
			await restoreHttpChallenge(certificate, httpState, true);
		}
	}
}

async function renewCertificateWithRetry(certificate) {
	let attempt = 0;
	while (true) {
		try {
			await renewCertificate(certificate);
			return;
		} catch (err) {
			if (!isCertbotBusyError(err) || attempt >= options.certbotLockRetries) {
				throw err;
			}
			attempt += 1;
			log(`#${certificate.id} Certbot est deja occupe, attente ${options.certbotLockWaitSeconds}s puis nouvel essai ${attempt}/${options.certbotLockRetries}`);
			await sleep(options.certbotLockWaitSeconds * 1000);
		}
	}
}

async function main() {
	const certificates = await certificateModel
		.query()
		.where("is_deleted", 0)
		.andWhere("provider", "letsencrypt")
		.orderBy("id");

	if (!certificates.length) {
		log("Aucun certificat Let's Encrypt actif trouve dans NPM.");
		return 0;
	}

	let toRenew = 0;
	let skipped = 0;
	let renewed = 0;
	let failed = 0;

	for (const certificate of certificates) {
		const info = await readCertificateInfo(certificate.id);
		const renew = shouldRenew(info);
		const marker = renew ? "A RENOUVELER" : "OK";
		const line = `#${certificate.id} ${marker} - ${formatDomains(certificate)} - ${formatRemaining(info.secondsRemaining)} - fin: ${formatDate(info.notAfter)} - raison: ${reasonFor(info)}`;

		if (options.listOnly || options.dryRun) {
			log(line);
			continue;
		}

		if (!renew) {
			skipped += 1;
			log(line);
			continue;
		}

		toRenew += 1;
		log(line);
		log(`#${certificate.id} renouvellement lance`);

		try {
			await renewCertificateWithRetry(certificate);
			const updatedInfo = await readCertificateInfo(certificate.id);
			renewed += 1;
			log(`#${certificate.id} OK - nouveau certificat valide jusqu'au ${formatDate(updatedInfo.notAfter)}`);
		} catch (err) {
			failed += 1;
			log(`#${certificate.id} ECHEC - ${err.message}`);
		}
	}

	if (options.listOnly || options.dryRun) {
		log("Mode lecture seule: aucun certificat n'a ete modifie.");
		return 0;
	}

	log(`Resume: ${renewed} renouvele(s), ${skipped} conserve(s), ${failed} erreur(s), ${toRenew} candidat(s).`);
	return failed > 0 ? 2 : 0;
}

try {
	const exitCode = await main();
	process.exit(exitCode);
} catch (err) {
	log(`ERREUR FATALE - ${err.stack || err.message}`);
	process.exit(2);
}
NODE
then
    log "Termine."
else
    status=$?
    log "Termine avec erreur (code $status)."
    exit "$status"
fi
