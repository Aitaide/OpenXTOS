/*
 * XTOS BCI Test Program
 *
 * Simple test application for BCI driver framework
 */

#include <stdio.h>
#include <stdlib.h>
#include <signal.h>
#include <unistd.h>
#include <xtos/bci.h>

static volatile int running = 1;

static void signal_handler(int sig)
{
	(void)sig;
	running = 0;
}

int main(int argc, char *argv[])
{
	xtos_bci_session_t *session;
	struct xtos_bci_device_info info;
	struct xtos_bci_statistics stats;
	float *buffer;
	int ret;
	const char *device = "/dev/xtos0";

	if (argc > 1)
		device = argv[1];

	printf("XTOS BCI Test Program\n");
	printf("=====================\n\n");

	/* Create session */
	printf("Opening device: %s\n", device);
	session = xtos_bci_session_create(device);
	if (!session) {
		perror("Failed to create session");
		return 1;
	}

	/* Get device info */
	ret = xtos_bci_get_info(session, &info);
	if (ret < 0) {
		perror("Failed to get device info");
		xtos_bci_session_destroy(session);
		return 1;
	}

	printf("\nDevice Information:\n");
	printf("  Name: %s\n", info.name);
	printf("  Device ID: %u\n", info.dev_id);
	printf("  Channels: %u\n", info.num_channels);
	printf("  Sampling Rate: %u Hz\n", info.sampling_rate);
	printf("  Resolution: %u bits\n", info.resolution_bits);
	printf("  Input Range: %u mV\n", info.input_range_mv);
	printf("\n");

	/* Allocate buffer */
	buffer = malloc(info.num_channels * sizeof(float));
	if (!buffer) {
		perror("Failed to allocate buffer");
		xtos_bci_session_destroy(session);
		return 1;
	}

	/* Start streaming */
	printf("Starting data stream...\n");
	ret = xtos_bci_start(session);
	if (ret < 0) {
		perror("Failed to start stream");
		free(buffer);
		xtos_bci_session_destroy(session);
		return 1;
	}

	/* Setup signal handler */
	signal(SIGINT, signal_handler);
	signal(SIGTERM, signal_handler);

	printf("Reading data (press Ctrl+C to stop)...\n\n");

	/* Read loop */
	int samples_count = 0;
	while (running) {
		ret = xtos_bci_read_samples(session, buffer, info.num_channels);
		if (ret < 0) {
			perror("Failed to read samples");
			break;
		}

		if (ret > 0) {
			samples_count++;

			/* Print every 10th sample */
			if (samples_count % 10 == 0) {
				printf("Sample #%d: ", samples_count);
				for (unsigned int i = 0; i < info.num_channels; i++) {
					printf("%.2f ", buffer[i]);
				}
				printf("µV\n");
			}
		}
	}

	/* Stop streaming */
	printf("\nStopping data stream...\n");
	xtos_bci_stop(session);

	/* Get statistics */
	ret = xtos_bci_get_stats(session, &stats);
	if (ret == 0) {
		printf("\nStatistics:\n");
		printf("  Samples read: %lu\n", stats.samples_read);
		printf("  Samples dropped: %lu\n", stats.samples_dropped);
		printf("  Errors: %lu\n", stats.errors);
		printf("  Buffer overruns: %lu\n", stats.overruns);
	}

	/* Cleanup */
	free(buffer);
	xtos_bci_session_destroy(session);

	printf("\nTest completed.\n");

	return 0;
}
