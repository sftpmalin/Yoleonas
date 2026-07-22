package com.sftpmalin.yoleo.monitoring;

import org.junit.Test;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public final class MonitoringTransitionsTest {
    @Test
    public void firstHighReadingSeedsSilently() {
        assertFalse(MonitoringTransitions.shouldNotifyHigh(false, false, 95, 90));
        assertTrue(MonitoringTransitions.updatedHighState(false, 95, 90));
    }

    @Test
    public void thresholdOnlyNotifiesOnCrossing() {
        assertTrue(MonitoringTransitions.shouldNotifyHigh(true, false, 90, 90));
        assertFalse(MonitoringTransitions.shouldNotifyHigh(true, true, 97, 90));
    }

    @Test
    public void hysteresisRequiresRealRecoveryBeforeRearming() {
        assertTrue(MonitoringTransitions.updatedHighState(true, 86, 90));
        assertFalse(MonitoringTransitions.updatedHighState(true, 84.9, 90));
    }

    @Test
    public void serviceOnlyNotifiesTrueToFalse() {
        assertFalse(MonitoringTransitions.becameFalse(false, true, false));
        assertTrue(MonitoringTransitions.becameFalse(true, true, false));
        assertFalse(MonitoringTransitions.becameFalse(true, false, false));
        assertFalse(MonitoringTransitions.becameFalse(true, false, true));
    }

    @Test
    public void containerOnlyNotifiesWhenRunningStopsOrDisappears() {
        assertTrue(MonitoringTransitions.stopped("running", "exited"));
        assertFalse(MonitoringTransitions.stopped("exited", "exited"));
        assertFalse(MonitoringTransitions.stopped(null, "exited"));
        assertTrue(MonitoringTransitions.disappeared("RUNNING"));
        assertFalse(MonitoringTransitions.disappeared("stopped"));
    }

    @Test
    public void taskOnlyNotifiesNewFailedExecution() {
        assertFalse(MonitoringTransitions.failedEventChanged(null, "v1", true));
        assertFalse(MonitoringTransitions.failedEventChanged("v1", "v1", true));
        assertFalse(MonitoringTransitions.failedEventChanged("v1", "v2", false));
        assertTrue(MonitoringTransitions.failedEventChanged("v1", "v2", true));
    }

    @Test
    public void buildOnlyNotifiesAnIncreaseAfterBaseline() {
        assertFalse(MonitoringTransitions.pendingIncreased(false, 0, 3));
        assertTrue(MonitoringTransitions.pendingIncreased(true, 0, 1));
        assertTrue(MonitoringTransitions.pendingIncreased(true, 2, 3));
        assertFalse(MonitoringTransitions.pendingIncreased(true, 3, 3));
        assertFalse(MonitoringTransitions.pendingIncreased(true, 3, 1));
    }
}
