import os
import subprocess
import time
import hashlib
import requests
import sys
import argparse

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='Flash OpenWRT firmware to router via TFTP')
    parser.add_argument('--version', required=True,
                      help='OpenWRT version (e.g. 23.05.5)')
    parser.add_argument('--arch', default='ramips',
                      help='Target architecture (default: ramips)')
    parser.add_argument('--board', default='mt7620',
                      help='Target board (default: mt7620)')
    parser.add_argument('--device', default='asus_rt-ac51u',
                      help='Device model (default: asus_rt-ac51u)')
    parser.add_argument('--sha256', 
                      help='Expected SHA256 checksum (optional)')
    parser.add_argument('--router-ip', default='192.168.1.1',
                      help='Router IP address (default: 192.168.1.1)')
    parser.add_argument('--local-ip', default='192.168.1.75',
                      help='Local IP address (default: 192.168.1.75)')
    return parser.parse_args()

def list_network_interfaces():
    """List all available network interfaces with their status."""
    try:
        result = subprocess.run(["nmcli", "device", "status"], 
                              capture_output=True, text=True, check=True)
        interfaces = []
        for line in result.stdout.splitlines()[1:]:  # Skip header
            parts = line.split()
            if len(parts) >= 3:
                device, type_, state = parts[0], parts[1], parts[2]
                if type_ in ["ethernet", "wifi"]:
                    interfaces.append((device, type_, state))
        return interfaces
    except subprocess.CalledProcessError as e:
        print(f"Error listing network interfaces: {e}")
        sys.exit(1)

def select_interface():
    """Let user select which interface to use."""
    interfaces = list_network_interfaces()
    if not interfaces:
        print("No suitable network interfaces found.")
        sys.exit(1)

    print("\nAvailable network interfaces:")
    for i, (device, type_, state) in enumerate(interfaces, 1):
        print(f"{i}. {device} ({type_}) - {state}")

    while True:
        try:
            choice = int(input("\nSelect interface number: ")) - 1
            if 0 <= choice < len(interfaces):
                return interfaces[choice][0]
            print("Invalid choice. Please try again.")
        except ValueError:
            print("Please enter a number.")

def configure_network_manager(interface, local_ip):
    """Configure NetworkManager connection with static IP."""
    print(f"Configuring NetworkManager for interface {interface}...")
    
    try:
        # Get current connection name
        conn_result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
            capture_output=True, text=True, check=True
        )
        
        # Find connection for our interface
        conn_name = None
        for line in conn_result.stdout.splitlines():
            name, dev = line.split(':')
            if dev == interface:
                conn_name = name
                break
        
        if not conn_name:
            # Create new connection if none exists
            conn_name = f"static-{interface}"
            subprocess.run([
                "sudo", "nmcli", "connection", "add",
                "type", "ethernet",
                "con-name", conn_name,
                "ifname", interface
            ], check=True)
        
        # Configure static IP
        print(f"Setting static IP {local_ip} on connection {conn_name}...")
        subprocess.run([
            "sudo", "nmcli", "connection", "modify",
            conn_name,
            "ipv4.method", "manual",
            "ipv4.addresses", f"{local_ip}/24"
        ], check=True)
        
        # Deactivate any other connections on this interface
        subprocess.run([
            "sudo", "nmcli", "device", "disconnect", interface
        ], check=True)
        
        # Activate the new connection
        subprocess.run([
            "sudo", "nmcli", "connection", "up", conn_name
        ], check=True)
        
        print(f"NetworkManager configuration complete for {interface}")
        
    except subprocess.CalledProcessError as e:
        print(f"Error configuring NetworkManager: {e}")
        sys.exit(1)

def verify_ip_configuration(interface, local_ip):
    """Verify that the IP was set correctly."""
    try:
        result = subprocess.run(
            ["ip", "addr", "show", interface],
            capture_output=True, text=True, check=True
        )
        if local_ip not in result.stdout:
            print(f"Warning: IP {local_ip} not found in interface configuration")
            print("Current interface configuration:")
            print(result.stdout)
            return False
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error verifying IP configuration: {e}")
        return False

def install_tftp_server():
    """Check if tftp is installed. If not, install and start the service."""
    tftp_path = "/usr/bin/tftp"
    print("Checking TFTP server availability...")
    
    if os.path.exists(tftp_path):
        print("TFTP server is already installed.")
    else:
        print(f"TFTP server ({tftp_path}) is not installed. Installing via pacman...")
        try:
            subprocess.run(["sudo", "pacman", "-S", "--noconfirm", "tftp-hpa"], check=True)
            print("TFTP server installed successfully.")
        except subprocess.CalledProcessError as e:
            print(f"Error installing TFTP server: {e}")
            sys.exit(1)
    
    # Configure and start TFTP socket service
    try:
        print("Configuring TFTP socket service...")
        
        # Enable and start the socket
        subprocess.run(["sudo", "systemctl", "enable", "tftpd.socket"], check=True)
        subprocess.run(["sudo", "systemctl", "start", "tftpd.socket"], check=True)
        
        # Check socket status
        result = subprocess.run(["systemctl", "is-active", "tftpd.socket"], 
                              capture_output=True, text=True)
        if result.stdout.strip() != "active":
            print("TFTP socket service is not active. Attempting to restart...")
            subprocess.run(["sudo", "systemctl", "restart", "tftpd.socket"], check=True)
            time.sleep(2)  # Give the service time to start
            
        print("TFTP socket service enabled and started.")
        
        # Verify the socket is listening
        netstat = subprocess.run(["sudo", "netstat", "-tulpn", "|", "grep", "tftp"],
                               shell=True, capture_output=True, text=True)
        if "69" not in netstat.stdout:  # TFTP uses port 69
            print("Warning: TFTP service may not be properly listening on port 69")
            print("You may need to manually verify the TFTP service status")
    except subprocess.CalledProcessError as e:
        print(f"Error configuring TFTP service: {e}")
        sys.exit(1)

def download_firmware(args):
    """Download firmware from OpenWrt server."""
    fw_url = (f"https://downloads.openwrt.org/releases/{args.version}/targets/"
             f"{args.arch}/{args.board}/openwrt-{args.version}-{args.arch}-"
             f"{args.board}-{args.device}-squashfs-sysupgrade.bin")
    
    tftp_path = os.path.dirname(os.path.abspath(__file__))
    fw_path = os.path.join(tftp_path, args.device)
    
    if os.path.exists(fw_path):
        print(f"Firmware file {args.device} already exists. Skipping download.")
        return fw_path
    
    print(f"Downloading firmware from {fw_url}...")
    try:
        response = requests.get(fw_url, stream=True)
        response.raise_for_status()
        
        with open(fw_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        print(f"Download complete. File saved as {fw_path}")
        return fw_path
    except requests.RequestException as e:
        print(f"Error downloading firmware: {e}")
        sys.exit(1)

def verify_sha256(file_path, expected_sha256):
    """Verify SHA256 checksum of downloaded file."""
    print("Verifying SHA256 checksum...")
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            sha256_hash.update(chunk)
    
    calculated_sha256 = sha256_hash.hexdigest()
    if calculated_sha256 == expected_sha256:
        print("Checksum verification successful.")
    else:
        print(f"Verification failed: expected {expected_sha256}, got {calculated_sha256}")
        sys.exit(1)

def get_network_interface():
    """Return name of first active network interface."""
    try:
        result = subprocess.run(["ip", "link", "show"], 
                              stdout=subprocess.PIPE, 
                              stderr=subprocess.PIPE, 
                              check=True, 
                              text=True)
        for line in result.stdout.splitlines():
            if "state UP" in line:
                return line.split(":")[1].strip()
    except subprocess.CalledProcessError as e:
        print(f"Error getting network interfaces: {e}")
        sys.exit(1)

def set_static_ip(local_ip):
    """Set static IP address using NetworkManager."""
    interface = select_interface()
    configure_network_manager(interface, local_ip)
    
    # Verify configuration
    if verify_ip_configuration(interface, local_ip):
        print(f"Static IP {local_ip} configured successfully on {interface}")
    else:
        print("IP configuration might not be correct. Please verify manually.")
        if input("Continue anyway? (y/n): ").lower() != 'y':
            sys.exit(1)

def automated_tftp_flash(router_ip, firmware_path):
    """Automatyczne flashowanie przez TFTP."""
    try:
        # Przygotuj plik firmware
        tftp_dir = os.path.dirname(os.path.abspath(firmware_path))
        sysupgrade_path = os.path.join(tftp_dir, "sysupgrade.bin")
        os.rename(firmware_path, sysupgrade_path)

        # Przygotuj skrypt TFTP
        tftp_script = f"""{router_ip}
binary
timeout 120
connects {router_ip}
put {sysupgrade_path}
quit
"""
        script_path = os.path.join(tftp_dir, "tftp_script")
        with open(script_path, "w") as f:
            f.write(tftp_script)

        # Uruchom TFTP z przygotowanym skryptem
        result = subprocess.run(
            ["tftp", "-v"],
            input=tftp_script.encode(),
            capture_output=True
        )

        if result.returncode != 0:
            print("TFTP transfer failed!")
            print("TFTP output:", result.stdout.decode())
            print("TFTP error:", result.stderr.decode())
            return False

        print("TFTP transfer completed")
        return True

    except Exception as e:
        print(f"Error during TFTP flashing: {e}")
        return False
    finally:
        # Przywróć oryginalną nazwę pliku
        if os.path.exists(sysupgrade_path):
            os.rename(sysupgrade_path, firmware_path)

def ask_to_continue(step):
    """Ask user if they want to continue with given step."""
    response = input(f"Continue with step: {step}? (y/n): ").lower()
    if response != 'y':
        print("Process terminated.")
        sys.exit(0)

def main():
    args = parse_arguments()
    
    ask_to_continue("Download OpenWRT firmware")
    firmware_path = download_firmware(args)

    if args.sha256:
        ask_to_continue("Verify SHA256 checksum")
        verify_sha256(firmware_path, args.sha256)

    ask_to_continue("Install TFTP server")
    install_tftp_server()

    ask_to_continue("Set static IP")
    set_static_ip(args.local_ip)

    # Automatyczne flashowanie
    if not automated_tftp_flash(args.router_ip, firmware_path):
        print("TFTP flashing failed")
        sys.exit(1)

    print(f"Flashing process complete. Wait for the router to reboot and try connecting to {args.router_ip}")

if __name__ == "__main__":
    main()
