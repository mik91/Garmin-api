<#
Publish the current master to the public repo as one snapshot commit.

The private history is never pushed: the snapshot has the same files as master, with the
public repo's last commit as its only parent. Before building it, every tracked file is
checked against .git/public-blocklist.txt (one word or phrase per line, matched without
case). That file lives inside .git, so it is never committed or published.

  .\scripts\publish-public.ps1                  # check and show what would change
  .\scripts\publish-public.ps1 -Push            # also push the snapshot
  .\scripts\publish-public.ps1 -Message "..."   # commit message (default: Release YYYY-MM-DD)
#>
param(
    [switch]$Push,
    [string]$Message = "Release $(Get-Date -Format 'yyyy-MM-dd')",
    [string]$Remote = "public"
)
$ErrorActionPreference = "Stop"
Set-Location (git rev-parse --show-toplevel)

function Fail($text) { Write-Host $text -ForegroundColor Red; exit 1 }

if ((git branch --show-current) -ne "master") { Fail "Switch to master first." }
if (git status --porcelain) { Fail "Commit or stash your changes first." }
git fetch -q origin
git fetch -q $Remote
if ((git rev-parse HEAD) -ne (git rev-parse origin/master)) { Fail "master differs from origin/master; pull or push first." }

$blocklist = Join-Path (git rev-parse --git-dir) "public-blocklist.txt"
if (-not (Test-Path $blocklist)) { Fail "Missing $blocklist (one private word or phrase per line)." }
$hits = git grep -n -i -I -F -f $blocklist HEAD -- .
if ($hits) {
    Write-Host "Private details found; nothing was published:" -ForegroundColor Red
    $hits | ForEach-Object { Write-Host "  $_" }
    exit 1
}

$tree = git rev-parse "HEAD^{tree}"
$parent = git rev-parse "$Remote/master"
if ((git rev-parse "$parent^{tree}") -eq $tree) { Write-Host "The public repo already matches master."; exit 0 }
$snapshot = git commit-tree $tree -p $parent -m $Message

Write-Host "Snapshot $($snapshot.Substring(0, 7)) on top of $Remote/master ($($parent.Substring(0, 7))):"
git diff --stat=120 $parent $snapshot | Select-Object -Last 1
git diff --name-status $parent $snapshot

if (-not $Push) { Write-Host "`nDry run. Run again with -Push to publish." -ForegroundColor Yellow; exit 0 }
git push $Remote "${snapshot}:refs/heads/master"
if ($LASTEXITCODE -ne 0) { Fail "Push failed." }
Write-Host "Published $($snapshot.Substring(0, 7)) to $Remote/master." -ForegroundColor Green
