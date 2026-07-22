package com.sftpmalin.yoleo.monitoring;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.concurrent.atomic.AtomicInteger;

public final class MonitoringRunGateTest {
    @Test
    public void secondRunIsCoalescedUntilFirstReleasesGate() {
        MonitoringRunGate gate = new MonitoringRunGate();

        MonitoringRunGate.Token first = gate.tryAcquire();

        assertNotNull(first);
        assertNull(gate.tryAcquire());

        first.release();
        assertNotNull(gate.tryAcquire());
    }

    @Test
    public void cancellationPreventsFurtherMutationButKeepsGateOwned() {
        MonitoringRunGate gate = new MonitoringRunGate();
        MonitoringRunGate.Token token = gate.tryAcquire();
        AtomicInteger mutations = new AtomicInteger();

        assertNotNull(token);
        assertTrue(token.runIfActive(mutations::incrementAndGet));

        token.cancel();

        assertTrue(token.isCancelled());
        assertFalse(token.runIfActive(mutations::incrementAndGet));
        assertEquals(1, mutations.get());
        assertNull(gate.tryAcquire());

        token.release();
        assertNotNull(gate.tryAcquire());
    }

    @Test
    public void releasingOldTokenTwiceDoesNotReleaseNewRun() {
        MonitoringRunGate gate = new MonitoringRunGate();
        MonitoringRunGate.Token first = gate.tryAcquire();
        assertNotNull(first);
        first.release();

        MonitoringRunGate.Token second = gate.tryAcquire();
        assertNotNull(second);

        first.release();

        assertNull(gate.tryAcquire());
        second.release();
        assertNotNull(gate.tryAcquire());
    }
}
