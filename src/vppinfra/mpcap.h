/*
 * Copyright (c) 2018 Cisco and/or its affiliates.
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at:
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * @file
 * @brief MPCAP utility definitions
 */
#ifndef included_vnet_mpcap_h
#define included_vnet_mpcap_h

#include <vlib/vlib.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <unistd.h>
#include <vppinfra/time_range.h>

/**
 * @brief Packet types supported by MPCAP
 *
 * null 0
 * ethernet 1
 * ppp 9
 * ip 12
 * hdlc 104
 */
#define foreach_vnet_mpcap_packet_type          \
  _ (null, 0)                                   \
  _ (ethernet, 1)                               \
  _ (ppp, 9)                                    \
  _ (ip, 12)                                    \
  _ (hdlc, 104)

typedef enum
{
#define _(f,n) MPCAP_PACKET_TYPE_##f = (n),
  foreach_vnet_mpcap_packet_type
#undef _
} mpcap_packet_type_t;

#define foreach_mpcap_file_header                       \
  /** 0xa1b2c3d4 host byte order.                       \
     0xd4c3b2a1 => need to byte swap everything. */     \
  _ (u32, magic)                                        \
                                                        \
  /** Currently major 2 minor 4. */                     \
  _ (u16, major_version)                                \
  _ (u16, minor_version)                                \
                                                        \
  /** 0 for GMT. */                                     \
  _ (u32, time_zone)                                    \
                                                        \
  /** Accuracy of timestamps.  Typically set to 0. */   \
  _ (u32, sigfigs)                                      \
                                                        \
  /** Size of largest packet in file. */                \
  _ (u32, max_packet_size_in_bytes)                     \
                                                        \
  /** One of vnet_mpcap_packet_type_t. */               \
  _ (u32, packet_type)

/** File header struct */
typedef struct
{
#define _(t, f) t f;
  foreach_mpcap_file_header
#undef _
} mpcap_file_header_t;

#define foreach_mpcap_packet_header             \
  /** Time stamp in seconds  */                 \
  _ (u32, time_in_sec)                          \
  /** Time stamp in microseconds. */            \
  _ (u32, time_in_usec)                         \
                                                \
  /** Number of bytes stored in file. */        \
  _ (u32, n_packet_bytes_stored_in_file)        \
  /** Number of bytes in actual packet. */	\
  _ (u32, n_bytes_in_packet)

/** Packet header. */
typedef struct
{
#define _(t, f) t f;
  foreach_mpcap_packet_header
#undef _
  /** Packet data follows. */
  u8 data[0];
} mpcap_packet_header_t;

/**
 * @brief MPCAP main state data structure
 */
typedef struct
{
  /** File name of mpcap output. */
  char *file_name;

  /** spinlock, initialized if flagged MPCAP_FLAG_THREAD_SAFE */
  clib_spinlock_t lock;

  /** Number of packets to capture. */
  u32 n_packets_to_capture;

  /** Packet type */
  mpcap_packet_type_t packet_type;

  /** Maximum file size */
  u64 max_file_size;

  /** Base address */
  u8 *file_baseva;

  /** current memory address */
  u8 *current_va;

  /** Number of packets currently captured. */
  u32 n_packets_captured;

  /** Pointer to file header in svm, for ease of updating */
  mpcap_file_header_t *file_header;

  /** flags */
  u32 flags;
#define MPCAP_FLAG_INIT_DONE (1 << 0)
#define MPCAP_FLAG_THREAD_SAFE (1 << 1)
#define MPCAP_FLAG_WRITE_ENABLE (1 << 2)

  /** Bytes written */
  u32 n_mpcap_data_written;

  /** Vector of mpcap data. */
  u8 *mpcap_data;

  /** Packets in mapped mpcap file. */
  u64 packets_read;

  /** Min/Max Packet bytes */
  u32 min_packet_bytes, max_packet_bytes;
} mpcap_main_t;

/* Some sensible default size */
#define MPCAP_DEFAULT_FILE_SIZE (10<<20)

/** initialize a mpcap file (for writing) */
clib_error_t *mpcap_init (mpcap_main_t * pm);

/** Flush / unmap a mpcap file */
clib_error_t *mpcap_close (mpcap_main_t * pm);

/** mmap a mpcap data file. */
clib_error_t *mpcap_map (mpcap_main_t * pm);

/**
 * @brief Add packet
 *
 * @param *pm - mpcap_main_t
 * @param time_now - f64
 * @param n_bytes_in_trace - u32
 * @param n_bytes_in_packet - u32
 *
 * @return Packet Data
 *
 */
static inline void *
mpcap_add_packet (mpcap_main_t * pm,
		  f64 time_now, u32 n_bytes_in_trace, u32 n_bytes_in_packet)
{
  mpcap_packet_header_t *h;
  u8 *d;

  /* File already closed? */
  if (PREDICT_FALSE (pm->flags & MPCAP_FLAG_INIT_DONE) == 0)
    return 0;

  d = pm->current_va;
  pm->current_va += sizeof (h[0]) + n_bytes_in_trace;

  /* Out of space? */
  if (PREDICT_FALSE (pm->current_va >= pm->file_baseva + pm->max_file_size))
    return 0;
  h = (void *) (d);
  h->time_in_sec = time_now;
  h->time_in_usec = 1e6 * (time_now - h->time_in_sec);
  h->n_packet_bytes_stored_in_file = n_bytes_in_trace;
  h->n_bytes_in_packet = n_bytes_in_packet;
  pm->n_packets_captured++;
  return h->data;
}

/**
 * @brief Add buffer (vlib_buffer_t) to the trace
 *
 * @param *pm - mpcap_main_t
 * @param *vm - vlib_main_t
 * @param time_now - f64
 * @param buffer_index - u32
 * @param n_bytes_in_trace - u32
 *
 */
static inline void
mpcap_add_buffer (mpcap_main_t * pm,
		  vlib_main_t * vm,
		  f64 time_now, u32 buffer_index, u32 n_bytes_in_trace)
{
  vlib_buffer_t *b = vlib_get_buffer (vm, buffer_index);
  u32 n = vlib_buffer_length_in_chain (vm, b);
  i32 n_left = clib_min (n_bytes_in_trace, n);
  void *d;

  clib_spinlock_lock_if_init (&pm->lock);

  d = mpcap_add_packet (pm, time_now, n_left, n);
  if (PREDICT_FALSE (d == 0))
    {
      mpcap_close (pm);
      clib_spinlock_unlock_if_init (&pm->lock);
      return;
    }

  while (1)
    {
      u32 copy_length = clib_min ((u32) n_left, b->current_length);
      clib_memcpy (d, b->data + b->current_data, copy_length);
      n_left -= b->current_length;
      if (n_left <= 0)
	break;
      d += b->current_length;
      ASSERT (b->flags & VLIB_BUFFER_NEXT_PRESENT);
      b = vlib_get_buffer (vm, b->next_buffer);
    }
  if (pm->n_packets_captured >= pm->n_packets_to_capture)
    mpcap_close (pm);

  clib_spinlock_unlock_if_init (&pm->lock);
}

#endif /* included_vnet_mpcap_h */

/*
 * fd.io coding-style-patch-verification: ON
 *
 * Local Variables:
 * eval: (c-set-style "gnu")
 * End:
 */
