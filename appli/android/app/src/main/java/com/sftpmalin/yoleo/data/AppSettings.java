package com.sftpmalin.yoleo.data;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

public final class AppSettings {
    public String serverUrl = "";
    public String username = "";
    public String p12DisplayName = "";
    public boolean configured;
    public int pollIntervalMinutes = 15;
    public int offlineFailureCount = 2;
    public boolean notifyServerOffline = true;
    public boolean notifyServerRecovery = true;
    public boolean notifyCpu = true;
    public int cpuThresholdPercent = 90;
    public boolean notifyRam = true;
    public int ramThresholdPercent = 90;
    public boolean notifyStorage = true;
    public int storageThresholdPercent = 80;
    public boolean notifyMountFailures = true;
    public boolean notifyDockerService = true;
    public boolean notifyDockerContainers = true;
    public boolean notifySamba = true;
    public boolean notifyTaskFailures = true;
    public boolean notifyBuildPending = true;
    public boolean notifyRegistryCleanup;
    public int registryReminderDay = 1;
    public boolean mountSelectionConfigured;
    public Set<String> displayedMountPaths = new LinkedHashSet<>();
    public Set<String> homeItems = defaultHomeItems();
    public List<String> navigationOrder = defaultNavigationOrder();
    public String lastFilePath = "/mnt";

    public static List<String> defaultNavigationOrder() {
        return new ArrayList<>(Arrays.asList(
                "home", "docker", "storage", "tasks", "files", "backup", "vms"));
    }

    public static Set<String> defaultHomeItems() {
        return new LinkedHashSet<>(Arrays.asList(
                "cpu", "ram", "storage", "temperatures", "fans", "gpus",
                "host", "network", "uptime", "services", "docker", "samba", "tasks", "vms", "build"));
    }

    public AppSettings copy() {
        AppSettings copy = new AppSettings();
        copy.serverUrl = serverUrl;
        copy.username = username;
        copy.p12DisplayName = p12DisplayName;
        copy.configured = configured;
        copy.pollIntervalMinutes = pollIntervalMinutes;
        copy.offlineFailureCount = offlineFailureCount;
        copy.notifyServerOffline = notifyServerOffline;
        copy.notifyServerRecovery = notifyServerRecovery;
        copy.notifyCpu = notifyCpu;
        copy.cpuThresholdPercent = cpuThresholdPercent;
        copy.notifyRam = notifyRam;
        copy.ramThresholdPercent = ramThresholdPercent;
        copy.notifyStorage = notifyStorage;
        copy.storageThresholdPercent = storageThresholdPercent;
        copy.notifyMountFailures = notifyMountFailures;
        copy.notifyDockerService = notifyDockerService;
        copy.notifyDockerContainers = notifyDockerContainers;
        copy.notifySamba = notifySamba;
        copy.notifyTaskFailures = notifyTaskFailures;
        copy.notifyBuildPending = notifyBuildPending;
        copy.notifyRegistryCleanup = notifyRegistryCleanup;
        copy.registryReminderDay = registryReminderDay;
        copy.mountSelectionConfigured = mountSelectionConfigured;
        copy.displayedMountPaths = new LinkedHashSet<>(displayedMountPaths);
        copy.homeItems = new LinkedHashSet<>(homeItems);
        copy.navigationOrder = new ArrayList<>(navigationOrder);
        copy.lastFilePath = lastFilePath;
        return copy;
    }
}
