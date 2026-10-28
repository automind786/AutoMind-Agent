import main

def start_cli():
    print("=" * 45)
    print("      AUTOMIND LOCAL CONTROLLER (OFFLINE)      ")
    print("=" * 45)
    print("Browser ki zaroorat nahi hai.")
    print("Task likhein aur Enter dabayein.")
    print("Band karne ke liye exit likhein.\n")

    while True:
        try:
            task = input("AutoMind >> ").strip()
            if not task:
                continue
            if task.lower() in ("exit", "quit", "q"):
                print("Exiting AutoMind...")
                break
            
            res = main.board_run_plan(task, progress=print)
            status = res.get("status", "unknown")
            source = res.get("source", "n/a")
            print(f"\n[STATUS]: {status} | [SOURCE]: {source}")
            if res.get("details"):
                print(f"[DETAILS]: {res.get("details")}")
            print("-" * 45)
        except (KeyboardInterrupt, EOFError):
            print("\nExiting AutoMind...")
            break

if __name__ == "__main__":
    start_cli()
