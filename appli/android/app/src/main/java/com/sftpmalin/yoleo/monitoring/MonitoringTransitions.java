package com.sftpmalin.yoleo.monitoring;

final class MonitoringTransitions {
    static final double HYSTERESIS_PERCENT = 5.0;

    private MonitoringTransitions() {
    }

    static boolean shouldNotifyHigh(
            boolean known,
            boolean wasHigh,
            double current,
            double threshold) {
        return known && !wasHigh && current >= threshold;
    }

    static boolean updatedHighState(boolean wasHigh, double current, double threshold) {
        if (current >= threshold) {
            return true;
        }
        if (current < threshold - HYSTERESIS_PERCENT) {
            return false;
        }
        return wasHigh;
    }

    static boolean becameFalse(boolean known, boolean previous, boolean current) {
        return known && previous && !current;
    }

    static boolean stopped(String previous, String current) {
        return isRunning(previous) && !isRunning(current);
    }

    static boolean disappeared(String previous) {
        return isRunning(previous);
    }

    static boolean failedEventChanged(
            String previousSignature,
            String currentSignature,
            boolean failed) {
        return failed && previousSignature != null &&
                !previousSignature.equals(currentSignature);
    }

    static boolean pendingIncreased(boolean known, int previous, int current) {
        return known && current > 0 && current > previous;
    }

    private static boolean isRunning(String state) {
        return state != null && "running".equalsIgnoreCase(state.trim());
    }
}
