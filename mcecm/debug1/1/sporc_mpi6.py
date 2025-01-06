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
    for x in range(N):
        for y in range(N):
            for z in range(N):
                spin = lattice_spins[x, y, z]
                if spin == 1:
                    strain_field[x, y, z] = epsilon_1
                elif spin == 2:
                    strain_field[x, y, z] = epsilon_2
                elif spin == 3:
                    strain_field[x, y, z] = epsilon_3
    return strain_field

# Compute elastic energy
def compute_elastic_energy(lattice_spins, q_grid, B, strain_ft, rank, size):
    N = lattice_spins.shape[0]
    local_energy = 0
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
def monte_carlo_step(
    lattice_spins, temperature, q_grid, B, strain_ft, epsilon_1, epsilon_2, epsilon_3, 
    rank, size, comm, current_energy
):
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

    lattice_size = 16
    epsilon_a = 0.1
    gamma_0 = 0.4
    anisoPar = 1
    num_steps = 5000

    epsilon_1, epsilon_2, epsilon_3 = define_dimensionless_strains(epsilon_a, gamma_0)
    stiffness_tensor = precompute_stiffness_tensor(anisoPar)
    q_grid, B = precompute_reciprocal_space_and_kernel(lattice_size, stiffness_tensor)

    for temp_index, temperature in enumerate(np.linspace(1.0, 0.1, 10)):
        lattice_spins = np.random.choice([1, 2, 3], size=(lattice_size, lattice_size, lattice_size))
        strain_field = compute_strain_field(lattice_spins, epsilon_1, epsilon_2, epsilon_3)
        strain_ft = np.fft.fftn(strain_field, axes=(0, 1, 2))
        terminate_flag = False
        zero_move_steps = 0
        prev_energy = None
        dErel_tolerance = 1e-10
        stable_steps = 0
        max_stable_steps = 50
        energy_log_file = open(f"total_energy_T{temperature:.2f}.txt", "w")

        local_energy = compute_elastic_energy(lattice_spins, q_grid, B, strain_ft, rank, size)
        current_energy = comm.reduce(local_energy, op=MPI.SUM, root=0)
        current_energy = comm.bcast(current_energy, root=0)

        for step in range(1, num_steps + 1):
            accepted_moves = 0
            prev_energy = current_energy
            for _ in range(lattice_spins.size // size):
               accepted, current_energy = monte_carlo_step(lattice_spins, temperature, q_grid, B, strain_ft, epsilon_1, epsilon_2, epsilon_3, rank, size, comm, current_energy)
               if accepted:
                  accepted_moves += 1

            if rank == 0:
                energy_log_file.write(f"{current_energy}\n")
                dErel = 0
                if prev_energy is not None:
                    dErel = abs(current_energy - prev_energy) / prev_energy
                    if dErel < dErel_tolerance:
                        stable_steps += 1
                    else:
                        stable_steps = 0

                print(f"Timestep {step}: totE={current_energy}, dErel = {dErel}, Accepted Moves = {accepted_moves}")

                if accepted_moves <= 0:
                    zero_move_steps += 1
                else:
                    zero_move_steps = 0

                if stable_steps >= max_stable_steps or zero_move_steps >= max_stable_steps:
                    terminate_flag = True
                    np.savetxt(f"final_spins_T{temperature:.2f}.txt", lattice_spins.reshape(-1), fmt='%d')
                    print(f"Simulation complete for T={temperature:.2f}. Final spins saved.")

            terminate_flag = comm.bcast(terminate_flag, root=0)
            if terminate_flag:
                break

        energy_log_file.close()
   # Save the spins at the maximum step if stabilization is not reached
        if not terminate_flag and rank == 0:
           print("Maximum steps reached without stabilization. Saving spins.")
           np.savetxt(f"maxStep_spins_T{temperature:.2f}.txt", lattice_spins.reshape(-1), fmt='%d')

if __name__ == "__main__":
    main()
