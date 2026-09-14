import time

messages = []

print("\n" + "=" * 50)
print("        WAR ROOM AI — INCIDENT MODE")
print("=" * 50)
print("I'm listening to the meeting.")
print("I'll collect everything said for 3 minutes.")
print("Type 'end' when the meeting is finished.\n")

start_time = time.time()

while True:
    speaker = input("Who is speaking? ")

    if speaker.lower() == "end":
        print("\nMeeting ended.")
        break

    message = input(f"{speaker}: ")

    messages.append({
        "speaker": speaker,
        "message": message
    })

    print("   Got it. I'm keeping track.\n")

    elapsed_time = time.time() - start_time

    if elapsed_time >= 180:
        print("\n" + "-" * 50)
        print("3 MINUTES HAVE PASSED")
        print("-" * 50)

        print("\nHere's what I heard:\n")

        for item in messages:
            print(f"{item['speaker']}: {item['message']}")

        print("\nI'm ready to analyze these messages.")

        messages = []
        start_time = time.time()

        print("\nStarting a new 3-minute window...\n")