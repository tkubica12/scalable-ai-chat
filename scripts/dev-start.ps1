# Local Development Runner Script for Scalable AI Chat
# This script starts specified services or all services by default

param(
    [string[]]$Services = @(),
    [switch]$Help
)

$AllServices = @(
    @{ Name = "front_service"; DisplayName = "Front Service"; Path = "src/front_service"; Port = 8000; Type = "Python" },
    @{ Name = "sse_service"; DisplayName = "SSE Service"; Path = "src/sse_service"; Port = 8002; Type = "Python" },
    @{ Name = "history_api"; DisplayName = "History API"; Path = "src/history_api"; Port = 8005; Type = "Python" },
    @{ Name = "memory_api"; DisplayName = "Memory API"; Path = "src/memory_api"; Port = 8006; Type = "Python" },
    @{ Name = "llm_worker"; DisplayName = "LLM Worker"; Path = "src/llm_worker"; Port = 0; Type = "Python" },
    @{ Name = "history_worker"; DisplayName = "History Worker"; Path = "src/history_worker"; Port = 0; Type = "Python" },
    @{ Name = "memory_worker"; DisplayName = "Memory Worker"; Path = "src/memory_worker"; Port = 0; Type = "Python" },
    @{ Name = "web_client"; DisplayName = "Web Client"; Path = "src/web_client"; Port = 5173; Type = "Node" }
)

function Show-Help {
    Write-Host "Scalable AI Chat - Local Development Starter" -ForegroundColor Green
    Write-Host ""
    Write-Host "Usage: .\dev-start.ps1 [Services...] [-Help]" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Services:" -ForegroundColor Cyan
    foreach ($service in $AllServices) {
        $portInfo = if ($service.Port -gt 0) { " (port $($service.Port))" } else { " (background worker)" }
        Write-Host "  $($service.Name.PadRight(17)) - $($service.DisplayName)$portInfo" -ForegroundColor White
    }
    Write-Host ""
    Write-Host "Examples:" -ForegroundColor Cyan
    Write-Host "  .\dev-start.ps1                                    # Start all services" -ForegroundColor White
    Write-Host "  .\dev-start.ps1 front_service sse_service         # Start specific services" -ForegroundColor White
    Write-Host "  .\dev-start.ps1 web_client                        # Start only web client" -ForegroundColor White
}

if ($Help) {
    Show-Help
    exit 0
}

# Colors for output
$Colors = @{
    Info = "Cyan"
    Success = "Green"
    Warning = "Yellow"
    Error = "Red"
    Service = "Magenta"
}

function Write-ColorOutput {
    param([string]$Message, [string]$Color = "White")
    Write-Host $Message -ForegroundColor $Colors[$Color]
}

function Test-Prerequisites {
    Write-ColorOutput "🔍 Checking prerequisites..." "Info"
    
    # Check if uv is installed
    try {
        $uvVersion = uv --version 2>$null
        Write-ColorOutput "✅ uv found: $uvVersion" "Success"
    }
    catch {
        Write-ColorOutput "❌ uv not found. Please install uv package manager." "Error"
        exit 1
    }
    
    # Check if npm is installed (only if web_client is in services to start)
    $needsNode = $Services.Count -eq 0 -or $Services -contains "web_client"
    if ($needsNode) {
        try {
            $npmVersion = npm --version 2>$null
            Write-ColorOutput "✅ npm found: v$npmVersion" "Success"
        }
        catch {
            Write-ColorOutput "❌ npm not found. Please install Node.js and npm for web_client." "Error"
            exit 1
        }
    }
    
    # Check if we're in the right directory
    if (!(Test-Path "src")) {
        Write-ColorOutput "❌ Not in project root directory. Please run from the scalable-ai-chat folder." "Error"
        exit 1
    }
    
    Write-ColorOutput "✅ All prerequisites met" "Success"
}

function Start-Service {
    param(
        [hashtable]$ServiceConfig
    )
    
    $serviceName = $ServiceConfig.DisplayName
    $path = $ServiceConfig.Path
    $port = $ServiceConfig.Port
    $type = $ServiceConfig.Type
    
    if ($port -gt 0) {
        Write-ColorOutput "🚀 Starting $serviceName on port $port..." "Service"
    } else {
        Write-ColorOutput "🚀 Starting $serviceName (background worker)..." "Service"
    }
      $processArgs = @{
        FilePath = if ($type -eq "Python") { "uv" } else { "npm.cmd" }
        ArgumentList = if ($type -eq "Python") { @("run", "python", "main.py") } else { @("run", "dev") }
        WorkingDirectory = $path
        WindowStyle = "Normal"
        PassThru = $true
    }
    
    $process = Start-Process @processArgs
    
    if ($process) {
        Write-ColorOutput "✅ $serviceName started (PID: $($process.Id))" "Success"
        return $process
    } else {
        Write-ColorOutput "❌ Failed to start $serviceName" "Error"
        return $null
    }
}

function Show-ServiceUrls {
    param([array]$RunningServices)
    
    Write-ColorOutput "`n🌐 Service URLs:" "Info"
    
    foreach ($serviceConfig in $RunningServices) {
        if ($serviceConfig.Port -gt 0) {
            Write-ColorOutput "📡 $($serviceConfig.DisplayName.PadRight(15)): http://localhost:$($serviceConfig.Port)" "Service"
        } else {
            Write-ColorOutput "⚙️  $($serviceConfig.DisplayName.PadRight(15)): (background worker)" "Service"
        }
    }
    
    Write-ColorOutput "`n💡 Health check endpoints available at /health on API services" "Info"
}

function Stop-AllServices {
    Write-ColorOutput "`n🛑 Stopping all services..." "Warning"
    
    # Find and stop uv and node processes started by this script
    Get-Process | Where-Object { $_.ProcessName -eq "uv" -or $_.ProcessName -eq "python" -or $_.ProcessName -eq "node" } | ForEach-Object {
        try {
            Stop-Process -Id $_.Id -Force
            Write-ColorOutput "✅ Stopped process $($_.ProcessName) (PID: $($_.Id))" "Success"
        }
        catch {
            Write-ColorOutput "⚠️  Could not stop process $($_.ProcessName) (PID: $($_.Id))" "Warning"
        }
    }
}

# Register cleanup on exit
Register-EngineEvent PowerShell.Exiting -Action {
    Stop-AllServices
}

# Handle Ctrl+C
$null = Register-ObjectEvent -InputObject ([System.Console]) -EventName CancelKeyPress -Action {
    Stop-AllServices
    exit 0
}

# Main execution
Write-ColorOutput "🚀 Scalable AI Chat - Local Development Environment" "Info"

Test-Prerequisites

# Determine which services to start
$servicesToStart = @()
if ($Services.Count -eq 0) {
    # Start all services
    $servicesToStart = $AllServices
    Write-ColorOutput "Starting all services..." "Info"
} else {
    # Start specific services
    foreach ($serviceName in $Services) {
        $serviceConfig = $AllServices | Where-Object { $_.Name -eq $serviceName }
        if ($serviceConfig) {
            $servicesToStart += $serviceConfig
        } else {
            Write-ColorOutput "❌ Unknown service: $serviceName" "Error"
            Write-ColorOutput "Available services: $($AllServices.Name -join ', ')" "Info"
            exit 1
        }
    }
    Write-ColorOutput "Starting selected services: $($Services -join ', ')" "Info"
}

# Start all services
$processes = @()
foreach ($serviceConfig in $servicesToStart) {
    $process = Start-Service -ServiceConfig $serviceConfig
    if ($process) {
        $processes += $process
    }
    Start-Sleep -Seconds 2  # Stagger startup
}

Show-ServiceUrls -RunningServices $servicesToStart

Write-ColorOutput "`n✨ All services started! Press Ctrl+C to stop all services." "Success"
Write-ColorOutput "📝 Check service logs in their respective terminal windows." "Info"

# Keep script running
try {
    while ($true) {
        Start-Sleep -Seconds 5
        
        # Check if processes are still running
        $runningCount = ($processes | Where-Object { !$_.HasExited }).Count
        if ($runningCount -eq 0) {
            Write-ColorOutput "⚠️  All services have stopped." "Warning"
            break
        }
    }
}
catch {
    Write-ColorOutput "`n🛑 Interrupted by user" "Warning"
}
finally {
    Stop-AllServices
}
