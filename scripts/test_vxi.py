import vxi11
import sys

# --- Configuration ---
# !!! IMPORTANT: Replace this with the actual IP address of your VXI-11 device.
DEVICE_IP = "127.0.0.1"
COMMAND_TO_SEND = "*IDN?" # Standard identification query
# --- End Configuration ---

def main():
    """
    Main function to connect, lock, query, and unlock a VXI-11 device.
    """
    print(f"Attempting to connect to device at: {DEVICE_IP}...")
    
    instr = None # Initialize instr to None
    try:
        # Manually create the instrument instance
        instr = vxi11.Instrument(DEVICE_IP, name="loopback0")
        print(f"Successfully connected to {DEVICE_IP} (instrument: loopback0).")

        try:
                # 1. Establish a lock on the device
                print("Acquiring lock...")
                instr.lock()
                print("Lock acquired.")

                # 2. Send the command and wait for a response
                # The .ask() method combines writing a command and reading the response.
                print(f"Sending command: {COMMAND_TO_SEND}")
                response = instr.ask(COMMAND_TO_SEND)

                # 3. Print the response
                # .strip() is used to remove any trailing newline characters
                print(f"\n--- Device Response ---")
                print(response.strip())
                print("-----------------------\n")

        except Exception as e:
                print(f"\nAn error occurred while communicating with the device: {e}", file=sys.stderr)
            
        finally:
                # 4. Explicitly unlock the device
                # This happens before the 'with' statement closes the connection.
                print("Releasing lock...")
                instr.unlock()
                print("Lock released.")

    except vxi11.vxi11.Vxi11Exception as e:
        print(f"\nError: Could not connect to device at {DEVICE_IP}.", file=sys.stderr)
        print(f"Details: {e}", file=sys.stderr)
        print("Please check the IP address and network connection.", file=sys.stderr)
    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}", file=sys.stderr)
    finally:
        # Ensure the connection is always closed if the instrument was created
        if instr:
            print("Closing connection...")
            instr.close()
            print("Connection closed.")

if __name__ == "__main__":
    main()


