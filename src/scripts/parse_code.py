from datetime import datetime
import os
import sys
from typing import Any

# Optional extension filtering
extensions = [".py", ".sh", ".yaml"]

# Exclude files by default
exclude_files = ["parse_code.py"]

# Optional exclude directories
exclude_dirs = [
    ".git",
    "__pycache__",
    "venv",
    "env",
    ".venv",
    ".env",
    "build",
    "dist",
    "node_modules",
    "tools",
]


def find_files_in_directory(
    project_path: str,
    extensions: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
    exclude_files: list[str] | None = None,
) -> list[str]:
    """Recursively find all files in a project directory with optional filtering.

    Args:
        project_path: Root path of the project
        extensions: List of file extensions to filter (e.g., ['.py', '.txt'])
        exclude_dirs: List of directory names to exclude from scanning
        exclude_files: List of file names to exclude from scanning

    Returns:
        List of relative file paths found
    """
    if not os.path.exists(project_path):
        print(f"Error: Directory {project_path} does not exist")
        return []

    found_files = []

    for root, dirs, files in os.walk(project_path):
        # Filter out excluded directories
        dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.startswith(".")]

        for file in files:
            if file.startswith("."):
                continue

            # Skip excluded files
            if file in exclude_files:
                continue

            full_path = os.path.join(root, file)
            relative_path = os.path.relpath(full_path, project_path)

            # Apply extension filter
            if extensions is None or any(file.endswith(ext) for ext in extensions):
                found_files.append(relative_path)

    return sorted(found_files)


def get_file_info(file_path: str) -> dict[str, Any]:
    """Retrieve metadata information for a file.

    Args:
        file_path: Absolute path to the file

    Returns:
        Dictionary containing file metadata (size, modification time)
    """
    try:
        stat = os.stat(file_path)
        return {
            "size_bytes": stat.st_size,
            "size_kb": round(stat.st_size / 1024, 2),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception:
        return {"size_bytes": 0, "size_kb": 0, "modified": "Unknown"}


def write_contents_to_file(
    files: list[str],
    project_path: str,
    output_file: str,
    verbose: bool = True,
    include_metadata: bool = True,
) -> dict[str, Any]:
    """Write the contents of all files to a single output file.

    Args:
        files: List of relative file paths
        project_path: Root path of the project
        output_file: Path to the output file
        verbose: Whether to show progress updates
        include_metadata: Whether to include file metadata in output

    Returns:
        Dictionary with processing statistics
    """
    stats = {
        "total_files": len(files),
        "processed_files": 0,
        "failed_files": 0,
        "total_size_bytes": 0,
    }

    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    with open(output_file, "w", encoding="utf-8") as out_file:
        for idx, relative_path in enumerate(files, 1):
            full_path = os.path.join(project_path, relative_path)

            if verbose and idx % 10 == 0:
                print(f"  Processing file {idx}/{len(files)}...")

            # Write file header
            out_file.write(f"# --- {relative_path}\n\n")

            # Include metadata if requested
            if include_metadata:
                info = get_file_info(full_path)
                out_file.write(f"# Size: {info['size_kb']} KB ({info['size_bytes']} bytes)\n")
                out_file.write(f"# Modified: {info['modified']}\n")
                stats["total_size_bytes"] += info["size_bytes"]

            # Read and write file content
            try:
                with open(full_path, encoding="utf-8") as in_file:
                    content = in_file.read()
                    out_file.write(content)

                    # Ensure proper formatting
                    if content and not content.endswith("\n"):
                        out_file.write("\n")

                    out_file.write("\n\n\n")

                    stats["processed_files"] += 1

            except UnicodeDecodeError:
                out_file.write(
                    "[ERROR: Could not read file as UTF-8. File might be binary or use different encoding]\n\n"
                )
                stats["failed_files"] += 1
                if verbose:
                    print(f"  ⚠️  Warning: Could not read {relative_path} (encoding issue)")

            except Exception as e:
                out_file.write(f"[ERROR: {e!s}]\n\n")
                stats["failed_files"] += 1
                if verbose:
                    print(f"  ❌ Error reading {relative_path}: {e!s}")

    return stats


def main() -> None:
    """Main entry point for the Python project file extractor."""
    print("=" * 60)
    print("PYTHON PROJECT FILE EXTRACTOR")
    print("=" * 60)
    print()

    # Use current directory as default project path
    project_path = os.getcwd()
    print(f"Default project path: {project_path}")

    if not os.path.exists(project_path):
        print(f"\n❌ Error: Path '{project_path}' does not exist")
        return

    # Metadata option
    include_metadata = False

    # Output file with default
    default_output = "src/scripts/code_dump.txt"
    output_file_input = input(f"Output filename (default: {default_output}): ").strip()
    output_file = output_file_input if output_file_input else default_output

    print(f"\n🔍 Scanning: {project_path}")

    print("📂 Finding files...")
    files = find_files_in_directory(project_path, extensions, exclude_dirs, exclude_files)

    if not files:
        print(f"\n❌ No files found in {project_path}")
        if extensions:
            print(f"   (Filtered by extensions: {', '.join(extensions)})")
        return

    print(f"\n✅ Found {len(files)} files:\n")

    # Display files grouped by directory
    files_by_dir = {}
    for file in files:
        dir_name = os.path.dirname(file) or "root"
        if dir_name not in files_by_dir:
            files_by_dir[dir_name] = []
        files_by_dir[dir_name].append(os.path.basename(file))

    for dir_name, file_list in sorted(files_by_dir.items()):
        print(f"  📁 {dir_name}/")
        for file in sorted(file_list):
            print(f"     └─ {file}")
        print()

    # Confirmation
    confirm = input(f"\n📝 Write all contents to '{output_file}'? (y/n): ").strip().lower()
    if confirm == "n":
        print("Operation cancelled.")
        return

    # Process and write files
    print("\n📖 Reading and writing contents...")
    stats = write_contents_to_file(
        files,
        project_path,
        output_file,
        verbose=True,
        include_metadata=include_metadata,
    )

    # Display summary
    print(f"\n{'=' * 60}")
    print("✅ OPERATION COMPLETED SUCCESSFULLY")
    print(f"{'=' * 60}")
    print("📊 Summary:")
    print(f"  • Total files found:     {stats['total_files']}")
    print(f"  • Files processed:       {stats['processed_files']}")
    print(f"  • Files with errors:     {stats['failed_files']}")
    print(
        f"  • Total size processed:  {stats['total_size_bytes'] / 1024:.2f} KB ({stats['total_size_bytes']} bytes)"
    )
    print(f"  • Output file:           {output_file}")
    print(f"  • Output size:           {os.path.getsize(output_file) / 1024:.2f} KB")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Operation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e!s}")
        sys.exit(1)
