# sbx notify daemon (runs on the Windows HOST, not inside the sandbox).
#
# Plays a .wav when the sandbox pings it over HTTP. The sandbox can reach the
# host ONLY as http://host.docker.internal:<port> and ONLY after a one-time
# `sbx policy allow network localhost:<port>`. Raw UDP/TCP/ICMP from the
# sandbox is blocked at the network layer, so this speaks minimal HTTP.
#
# Run:      powershell -NoProfile -ExecutionPolicy Bypass -File notify-daemon.ps1
# Autostart: register as a logon task (see README).

$Port = 53999

# .wav files live in ./sounds next to this script (gitignored — drop your own).
$SoundsDir = Join-Path $PSScriptRoot 'sounds'
$Sounds = @{
  '/stop'   = Join-Path $SoundsDir 'stop.wav'
  '/prompt' = Join-Path $SoundsDir 'prompt.wav'
}

$CRLF = [char]13 + [char]10

$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::IPv6Any, $Port)
$listener.Server.SetSocketOption(
  [System.Net.Sockets.SocketOptionLevel]::IPv6,
  [System.Net.Sockets.SocketOptionName]::IPv6Only, 0)
$listener.Start()
Write-Host "[sbx-notify] listening on 127.0.0.1:$Port"

try {
  while ($true) {
    $client = $listener.AcceptTcpClient()
    try {
      $stream = $client.GetStream()
      $buf = New-Object byte[] 2048
      Start-Sleep -Milliseconds 30
      $n = $stream.Read($buf, 0, $buf.Length)
      $reqLine = ([Text.Encoding]::ASCII.GetString($buf, 0, $n) -split "`n")[0].Trim()

      $path = ''
      if ($reqLine -match '^\S+\s+(\S+)') { $path = ($Matches[1] -split '\?')[0] }
      $wav = $Sounds[$path]

      $status = if ($wav) { '200 OK' } else { '404 Not Found' }
      $body   = if ($wav) { 'ok' } else { 'no' }
      $resp = "HTTP/1.1 $status" + $CRLF + "Content-Length: $($body.Length)" + $CRLF +
              "Connection: close" + $CRLF + $CRLF + $body
      $rb = [Text.Encoding]::ASCII.GetBytes($resp)
      $stream.Write($rb, 0, $rb.Length); $stream.Flush()
      $client.Close()

      if ($wav) {
        if (Test-Path -LiteralPath $wav) {
          (New-Object System.Media.SoundPlayer $wav).PlaySync()
        } else {
          Write-Host "[sbx-notify] wav not found: $wav"
        }
      } else {
        Write-Host "[sbx-notify] ignored path: $path"
      }
    } catch {
      Write-Host "[sbx-notify] err: $($_.Exception.Message)"
      try { $client.Close() } catch {}
    }
  }
} finally {
  $listener.Stop()
}
