namespace YoleoAgent;

internal static class Program
{
    [STAThread]
    private static async Task Main(string[] args)
    {
        if (args.Any(argument => string.Equals(argument, "--self-test", StringComparison.OrdinalIgnoreCase)))
        {
            Environment.ExitCode = SelfTest.Run();
            return;
        }

        if (args.Any(argument => string.Equals(argument, "--connection-test", StringComparison.OrdinalIgnoreCase)))
        {
            Environment.ExitCode = await ConnectionDiagnostics.RunAsync();
            return;
        }

        using var singleInstance = new Mutex(true, @"Local\YoleoAgent", out var isFirstInstance);
        if (!isFirstInstance)
        {
            MessageBox.Show(
                "Yoleo Agent est déjà lancé près de l'heure.",
                "Yoleo Agent",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
            return;
        }

        ApplicationConfiguration.Initialize();
        Application.Run(new YoleoApplicationContext());
    }
}
