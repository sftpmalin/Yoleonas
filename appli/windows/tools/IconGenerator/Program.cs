using System.Drawing.Drawing2D;
using System.Drawing.Imaging;

if (args.Length != 2)
{
    Console.Error.WriteLine("Usage: IconGenerator <logo.png> <App.ico>");
    return 1;
}

var inputPath = Path.GetFullPath(args[0]);
var outputPath = Path.GetFullPath(args[1]);
if (!File.Exists(inputPath))
{
    Console.Error.WriteLine("Logo introuvable: " + inputPath);
    return 1;
}

Directory.CreateDirectory(Path.GetDirectoryName(outputPath)!);
using var source = new Bitmap(inputPath);
var sizes = new[] { 16, 20, 24, 32, 40, 48, 64, 128, 256 };
var frames = new List<byte[]>();

foreach (var size in sizes)
{
    using var canvas = new Bitmap(size, size, PixelFormat.Format32bppArgb);
    using (var graphics = Graphics.FromImage(canvas))
    {
        graphics.Clear(Color.Transparent);
        graphics.CompositingMode = CompositingMode.SourceOver;
        graphics.CompositingQuality = CompositingQuality.HighQuality;
        graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
        graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
        graphics.SmoothingMode = SmoothingMode.HighQuality;

        var margin = Math.Max(1, size / 64);
        var available = size - margin * 2;
        var scale = Math.Min((double)available / source.Width, (double)available / source.Height);
        var width = Math.Max(1, (int)Math.Round(source.Width * scale));
        var height = Math.Max(1, (int)Math.Round(source.Height * scale));
        var x = (size - width) / 2;
        var y = (size - height) / 2;
        graphics.DrawImage(source, new Rectangle(x, y, width, height));
    }

    using var frame = new MemoryStream();
    canvas.Save(frame, ImageFormat.Png);
    frames.Add(frame.ToArray());
}

using var output = File.Create(outputPath);
using var writer = new BinaryWriter(output);
writer.Write((ushort)0);
writer.Write((ushort)1);
writer.Write((ushort)frames.Count);

var offset = 6 + frames.Count * 16;
for (var index = 0; index < frames.Count; index++)
{
    var size = sizes[index];
    writer.Write((byte)(size >= 256 ? 0 : size));
    writer.Write((byte)(size >= 256 ? 0 : size));
    writer.Write((byte)0);
    writer.Write((byte)0);
    writer.Write((ushort)1);
    writer.Write((ushort)32);
    writer.Write(frames[index].Length);
    writer.Write(offset);
    offset += frames[index].Length;
}

foreach (var frame in frames)
{
    writer.Write(frame);
}

Console.WriteLine($"Icône créée: {outputPath} ({frames.Count} tailles)");
return 0;
