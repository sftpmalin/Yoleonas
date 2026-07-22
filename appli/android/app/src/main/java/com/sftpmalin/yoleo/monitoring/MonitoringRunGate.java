package com.sftpmalin.yoleo.monitoring;

import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Autorise un seul contrôle en vol, quel que soit l'identifiant du JobScheduler.
 * Le jeton protège aussi les mutations finales contre une annulation concurrente.
 */
final class MonitoringRunGate {
    private Token active;

    synchronized Token tryAcquire() {
        if (active != null) {
            return null;
        }
        active = new Token(this);
        return active;
    }

    private synchronized void release(Token token) {
        if (active == token) {
            active = null;
        }
    }

    static final class Token {
        private final MonitoringRunGate owner;
        private final AtomicBoolean released = new AtomicBoolean(false);
        private final Object cancellationLock = new Object();
        private boolean cancelled;

        private Token(MonitoringRunGate owner) {
            this.owner = owner;
        }

        boolean isCancelled() {
            synchronized (cancellationLock) {
                return cancelled;
            }
        }

        void cancel() {
            synchronized (cancellationLock) {
                cancelled = true;
            }
        }

        /**
         * Exécute une mutation uniquement si le travail est encore actif. cancel()
         * utilise le même verrou, donc aucune mutation ne peut commencer après son retour.
         */
        boolean runIfActive(Runnable mutation) {
            synchronized (cancellationLock) {
                if (cancelled) {
                    return false;
                }
                mutation.run();
                return true;
            }
        }

        void release() {
            if (released.compareAndSet(false, true)) {
                owner.release(this);
            }
        }
    }
}
