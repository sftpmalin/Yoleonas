package com.sftpmalin.yoleo.data;

import org.junit.Test;

import java.util.Arrays;
import java.util.LinkedHashSet;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotSame;

public final class AppSettingsTest {
    @Test
    public void defaultHomeOrderContainsEveryAvailableBlockExactlyOnce() {
        assertEquals(
                AppSettings.defaultHomeItems(),
                new LinkedHashSet<>(AppSettings.defaultHomeOrder()));
        assertEquals(
                AppSettings.defaultHomeOrder().size(),
                AppSettings.defaultHomeItems().size());
    }

    @Test
    public void copyKeepsASeparateCustomHomeOrder() {
        AppSettings settings = new AppSettings();
        settings.homeOrder = Arrays.asList("ram", "cpu", "temperatures");

        AppSettings copy = settings.copy();

        assertEquals(settings.homeOrder, copy.homeOrder);
        assertNotSame(settings.homeOrder, copy.homeOrder);
    }
}
