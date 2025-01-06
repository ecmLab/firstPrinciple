from mpi4py import MPI
import numpy as np
import random

comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

# Define the new dimensionless lattice misfit strains
def define_dimensionless_strains(epsilon_a, gamma_0):
    factor = epsilon_a / gamma_0
    epsilon_1 = np.array([[1 + factor, 0, 0], [0, factor, 0], [0, 0, factor]])
    epsilon_2 = np.array([[factor, 0, 0], [0, 1 + factor, 0], [0, 0, factor]])
    epsilon_3 = np.array([[factor, 0, 0], [0, factor, 0], [0, 0, 1 + factor]])
    return epsilon_1, epsilon_2, epsilon_3

# Precompute stiffness tensor for cubic symmetry
def precompute_stiffness_tensor(anisoPar):
    C11 = 4 / anisoPar
    C12 = C11 / 2
    C44 = 1
    C = np.zeros((3, 3, 3, 3))
    for i in range(3):
        for j in range(3):
            for k in range(3):
                for l in range(3):
                    if i == j and k == l:
                        C[i, j, k, l] = C11 if i == k else C12
                    elif i == k and j == l:
                        C[i, j, k, l] = C44 if i != j else 0
    return C

# Precompute reciprocal space grid and kernel
def precompute_reciprocal_space_and_kernel(N, stiffness_tensor):
    q_range = np.fft.fftfreq(N, d=1 / (2 * np.pi))
    qx, qy, qz = np.meshgrid(q_range, q_range, q_range, indexing="ij")
    q_grid = np.stack((qx, qy, qz), axis=-1)
    B = np.zeros((N, N, N, 3, 3, 3, 3))
    for qx_idx in range(N):
        for qy_idx in range(N):
            for qz_idx in range(N):
                k = q_grid[qx_idx, qy_idx, qz_idx]
                if np.linalg.norm(k) == 0:
                    continue
                k_unit = k / np.linalg.norm(k)
                G_inv = np.einsum("ijmn,m,n->ij", stiffness_tensor, k_unit, k_unit)
                G = np.linalg.inv(G_inv) if np.linalg.det(G_inv) != 0 else np.zeros((3, 3))
                for i in range(3):
                    for j in range(3):
                        for k in range(3):
                            for l in range(3):
                                B[qx_idx, qy_idx, qz_idx, i, j, k, l] = (
                                    stiffness_tensor[i, j, k, l]
                                    - np.einsum(
                                        "mn,im,nj->",
                                        G,
                                        stiffness_tensor[:, :, i, j],
                                        stiffness_tensor[:, :, k, l],
                                    )
                                )
    return q_grid, B

# Compute strain field using dimensionless strain tensors
def compute_strain_field(lattice_spins, epsilon_1, epsilon_2, epsilon_3):
    N = lattice_spins.shape[0]
    strain_field = np.zeros((N, N, N, 3, 3))
    for ix in range(N):
        for iy in range(N):
            for iz in range(N):
                spin = lattice_spins[ix, iy, iz]
                if spin == 1:
                    strain_field[ix, iy, iz] = epsilon_1
                elif spin == 2:
                    strain_field[ix, iy, iz] = epsilon_2
                elif spin == 3:
                    strain_field[ix, iy, iz] = epsilon_3
    return strain_field

# Compute elastic energy
def compute_elastic_energy(lattice_spins, q_grid, B, strain_ft, rank, size):
    N = lattice_spins.shape[0]
    local_energy = 0
    # Parallelize over wavevectors
    for qx_idx in range(rank, N, size):  # Divide work among ranks
        for qy_idx in range(N):
            for qz_idx in range(N):
                if np.allclose(q_grid[qx_idx, qy_idx, qz_idx], 0):
                    continue
                local_energy += np.real(
                    np.einsum(
                        "ij,ijkl,kl->",
                        strain_ft[qx_idx, qy_idx, qz_idx],
                        B[qx_idx, qy_idx, qz_idx],
                        strain_ft[qx_idx, qy_idx, qz_idx].conj(),
                    )
                )
    return local_energy

# Monte Carlo step with MPI
def monte_carlo_step(lattice_spins, temperature, q_grid, B, strain_ft, epsilon_1, epsilon_2, epsilon_3, rank, size, comm, current_energy):
    N = lattice_spins.shape[0]
    x, y, z = np.random.randint(0, N, size=3)
    current_spin = lattice_spins[x, y, z]
    proposed_spin = random.choice([s for s in [1, 2, 3] if s != current_spin])

    # Compute new energy (local contribution for proposed spin)
    lattice_spins[x, y, z] = proposed_spin
    new_strain_field = compute_strain_field(lattice_spins, epsilon_1, epsilon_2, epsilon_3)
    strain_ft_new = np.fft.fftn(new_strain_field, axes=(0, 1, 2))
    local_E_new = compute_elastic_energy(lattice_spins, q_grid, B, strain_ft_new, rank, size)

    # Get total E_new across all ranks
    E_new = comm.reduce(local_E_new, op=MPI.SUM, root=0)
    E_new = comm.bcast(E_new, root=0)  # Broadcast total energy to all ranks

    # Calculate energy difference
    delta_E = E_new - current_energy
    max_exp_arg = 700

    # Metropolis acceptance criterion
    if delta_E > 0:
        prob = np.exp(-np.clip(delta_E / temperature, None, max_exp_arg))
    else:
        prob = 1.0

    if random.random() < prob:
        # Accept the proposed spin
        strain_ft[:] = strain_ft_new  # Update the Fourier transform of the strain field
        return True, E_new  # Accepted: return new energy
    else:
        # Revert to the original spin
        lattice_spins[x, y, z] = current_spin
        return False, current_energy  # Rejected: return current energy

# Main simulation
def main():

    lattice_size = 12

    # Load the spins from the file instead of random initialization
    try:
        lattice_spins = np.loadtxt("spins.txt", dtype=int).reshape((lattice_size, lattice_size, lattice_size))
        if rank == 0:
            print(f"Loaded lattice spins from file.")
    except Exception as e:
        if rank == 0:
            print(f"Error loading lattice spins from file: {e}")
        return

    epsilon_a = 0.1
    gamma_0 = 0.4
    anisoPar = 1
    temperature = 0.1
    num_steps = 1

    epsilon_1, epsilon_2, epsilon_3 = define_dimensionless_strains(epsilon_a, gamma_0)
    stiffness_tensor = precompute_stiffness_tensor(anisoPar)
    q_grid, B = precompute_reciprocal_space_and_kernel(lattice_size, stiffness_tensor)

    strain_field = compute_strain_field(lattice_spins, epsilon_1, epsilon_2, epsilon_3)
    strain_ft = np.fft.fftn(strain_field, axes=(0, 1, 2))

    if rank == 0:
       strain_tt = compute_strain_field(lattice_spins, epsilon_1, epsilon_2, epsilon_3)
       strain_ftt = np.fft.fftn(strain_tt, axes=(0, 1, 2))
       np.savetxt(f"spins1.txt", lattice_spins.reshape(-1), fmt='%d')
       np.savetxt(f"strain1.txt", strain_tt.reshape(-1,3), fmt='%f')
       np.savetxt(f"strainft1.txt", strain_ftt.reshape(-1,3), fmt='%f')

    local_energy = compute_elastic_energy(lattice_spins, q_grid, B, strain_ft, rank, size)
    current_energy = comm.reduce(local_energy, op=MPI.SUM, root=0)
    current_energy = comm.bcast(current_energy, root=0)

    # Initialize variables for energy stabilization
    dErel_tolerance = 1e-10 # Define the energy cutoff threshold
    stable_steps = 0  # Track successive steps with stable energy
    max_stable_steps = 10  # Number of steps to check for stability

    zero_move_steps = 0  # Track successive steps with zero accepted moves
    terminate_flag = False  # Termination flag shared across all ranks

    for step in range(1, num_steps + 1):
        accepted_moves = 0
        prev_energy = current_energy

        for _ in range(lattice_spins.size // size):
            accepted, current_energy = monte_carlo_step(lattice_spins, temperature, q_grid, B, strain_ft, epsilon_1, epsilon_2, epsilon_3, rank, size, comm, current_energy)
            if accepted:
               accepted_moves += 1

        if rank == 0:

            # Check energy stabilization
            dErel = abs(current_energy - prev_energy)/prev_energy
            if dErel < dErel_tolerance:
               stable_steps += 1
               print(f"Energy stable for {stable_steps} steps. Change = {dErel}")
            else:
               stable_steps = 0

            print(f"Timestep {step}: totE={current_energy}, dErel = {dErel}, Accepted Moves = {accepted_moves}")

            # Check termination due to zero moves
            if accepted_moves <= 0:
                zero_move_steps += 1
                print(f"Zero accepted moves for {zero_move_steps} successive steps.")
            else:
                zero_move_steps = 0

            # Terminate if energy is stable or zero moves persist
            if stable_steps >= max_stable_steps:
                print("Termination criteria met: Energy stabilized.")
                terminate_flag = True
            elif zero_move_steps >= max_stable_steps:
                print("Termination criteria met: Zero accepted moves for five successive steps.")
                terminate_flag = True

            if terminate_flag:
                print("Simulation complete. Final spins saved to 'finalS.txt'.")

        # Broadcast termination flag to all ranks
        terminate_flag = comm.bcast(terminate_flag, root=0)
        if terminate_flag:
            break

   # Save the spins at the maximum step if stabilization is not reached
    if not terminate_flag and rank == 0:
       print("Maximum steps reached without stabilization. Saving spins.")

if __name__ == "__main__":
    main()
